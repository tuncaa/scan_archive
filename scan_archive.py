from pathlib import Path
from datetime import datetime, timedelta
from contextlib import contextmanager
import argparse
import calendar
import hashlib
import logging
import msvcrt
import os
import shutil
import sqlite3
import time
import uuid

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
import pymupdf


# ---------- 設定：実際の環境に合わせて変更 ----------
SOURCE = Path(r"C:\tmp\scan-tmp\moto")  # スキャナーの保存先
DEST = Path(r"C:\tmp\scan-tmp\saki")              # PDF・TXTの保管先

TESSDATA = Path(__file__).resolve().parent / "tessdata"
LANGUAGE = "jpn+eng"
DPI = 300
THUMBNAIL_DPI = 150
OCR_PAGES = 1  # 先頭からOCRするページ数。-1で全ページ

STABLE_SECONDS = 10   # 一覧取得後、この秒数待ってサイズ・日時を再確認
MIN_TEXT_CHARS = 10   # これ未満なら「要確認」として保存
# -------------------------------------------------

SYSTEM = DEST / "_system"
WORK = DEST / "_work"
DATABASE = SYSTEM / "index.sqlite"


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def timestamp_iso(seconds):
    return datetime.fromtimestamp(seconds).astimezone().isoformat(
        timespec="seconds"
    )


def normalize_ocr_text(text):
    return text.replace(" ", "").replace("\u3000", "")


def fingerprint(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_inside(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


@contextmanager
def single_instance():
    """Windowsのファイルロックで二重起動を防止。"""
    lock_path = SYSTEM / "archive.lock"
    with lock_path.open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()

        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("別の処理が実行中です。") from exc

        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def open_database():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row

    connection.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            sha256 TEXT PRIMARY KEY,
            pdf_path TEXT NOT NULL,
            txt_path TEXT NOT NULL,
            original_path TEXT NOT NULL,
            original_created TEXT NOT NULL,
            original_modified TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            body TEXT,
            error TEXT,
            processed_at TEXT,
            thumbnail_path TEXT,
            note TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS source_files (
            source_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (source_path, sha256)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            is_hidden INTEGER NOT NULL DEFAULT 0,
            is_priority INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS document_tags (
            sha256 TEXT NOT NULL REFERENCES documents(sha256)
                ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id)
                ON DELETE CASCADE,
            PRIMARY KEY (sha256, tag_id)
        );
    """)

    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(documents)")
    }
    if "thumbnail_path" not in columns:
        connection.execute(
            "ALTER TABLE documents ADD COLUMN thumbnail_path TEXT"
        )
    if "note" not in columns:
        connection.execute(
            "ALTER TABLE documents ADD COLUMN note TEXT NOT NULL DEFAULT ''"
        )

    tag_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(tags)")
    }
    if "is_hidden" not in tag_columns:
        connection.execute(
            "ALTER TABLE tags ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0"
        )
    if "is_priority" not in tag_columns:
        connection.execute(
            "ALTER TABLE tags ADD COLUMN is_priority INTEGER NOT NULL DEFAULT 0"
        )

    connection.execute("PRAGMA foreign_keys = ON")
    connection.commit()
    return connection


def collect_candidates():
    candidates = []

    for path in SOURCE.rglob("*"):
        try:
            if path.suffix.lower() != ".pdf":
                continue
            if not path.is_file() or is_inside(path, DEST):
                continue

            candidates.append((path, fingerprint(path)))
        except OSError:
            logging.exception("候補ファイルの確認失敗: %s", path)

    return candidates


def thumbnail_path_for(pdf_path):
    return pdf_path.with_suffix(".png")


def create_thumbnail(pdf_path, thumbnail_path):
    if thumbnail_path.exists():
        return

    with pymupdf.open(pdf_path) as document:
        if document.needs_pass:
            raise RuntimeError("パスワード付きPDFです。")
        if document.page_count == 0:
            raise RuntimeError("ページがありません。")

        matrix = pymupdf.Matrix(
            THUMBNAIL_DPI / 72,
            THUMBNAIL_DPI / 72,
        )
        pixmap = document[0].get_pixmap(matrix=matrix, alpha=False)

    temporary = WORK / f"{thumbnail_path.name}.part"
    try:
        pixmap.save(str(temporary), output="png")
        os.replace(temporary, thumbnail_path)
        logging.info("サムネイル保存: %s", thumbnail_path)
    finally:
        temporary.unlink(missing_ok=True)


def import_pdf(connection, source, expected):
    if fingerprint(source) != expected:
        logging.info("書き込み中のため延期: %s", source)
        return

    stat = source.stat()
    digest = sha256_file(source)

    if fingerprint(source) != expected:
        logging.info("ハッシュ計算中に変更されたため延期: %s", source)
        return

    existing = connection.execute(
        "SELECT * FROM documents WHERE sha256 = ?",
        (digest,),
    ).fetchone()

    if existing:
        pdf_path = Path(existing["pdf_path"])
        txt_path = Path(existing["txt_path"])
    else:
        date = datetime.fromtimestamp(stat.st_mtime).astimezone()
        folder = DEST / date.strftime("%Y") / date.strftime("%m")

        # 衝突を避けるため、ファイル名には完全なSHA-256を使用
        stem = f"{date:%Y%m%d_%H%M%S}_{digest}"
        pdf_path = folder / f"{stem}.pdf"
        txt_path = folder / f"{stem}.txt"

    thumbnail_path = (
        Path(existing["thumbnail_path"])
        if existing and existing["thumbnail_path"]
        else thumbnail_path_for(pdf_path)
    )
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 既存PDFが欠落・破損していればコピー元から復旧
    needs_copy = (
        not pdf_path.exists()
        or sha256_file(pdf_path) != digest
    )

    if needs_copy:
        temporary = WORK / f"{digest}.pdf.part"
        try:
            shutil.copy2(source, temporary)

            if fingerprint(source) != expected:
                raise RuntimeError("コピー中にコピー元が変更されました。")

            if sha256_file(temporary) != digest:
                raise RuntimeError("コピー後のハッシュが一致しません。")

            # 同じ保存先ドライブ内で、一時ファイルから確定名に変更
            os.replace(temporary, pdf_path)
            logging.info("PDF保存: %s", pdf_path)
        finally:
            temporary.unlink(missing_ok=True)

    create_thumbnail(pdf_path, thumbnail_path)

    if not existing:
        # Python 3.12以降ではWindowsの作成日時にst_birthtimeを使用
        created = getattr(stat, "st_birthtime", stat.st_ctime)

        connection.execute("""
            INSERT INTO documents (
                sha256, pdf_path, txt_path, original_path,
                original_created, original_modified, imported_at,
                thumbnail_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            digest,
            str(pdf_path),
            str(txt_path),
            str(source.resolve()),
            timestamp_iso(created),
            timestamp_iso(stat.st_mtime),
            now_iso(),
            str(thumbnail_path),
        ))

    elif not txt_path.exists() or existing["thumbnail_path"] != str(
        thumbnail_path
    ):
        connection.execute("""
            UPDATE documents
            SET status = CASE
                    WHEN ? THEN 'pending'
                    ELSE status
                END,
                error = CASE
                    WHEN ? THEN NULL
                    ELSE error
                END,
                thumbnail_path = ?
            WHERE sha256 = ?
        """, (
            not txt_path.exists(),
            not txt_path.exists(),
            str(thumbnail_path),
            digest,
        ))

    connection.execute("""
        INSERT INTO source_files (source_path, sha256, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(source_path, sha256)
        DO UPDATE SET last_seen = excluded.last_seen
    """, (str(source.resolve()), digest, now_iso()))

    connection.commit()


def process_thumbnails(connection):
    rows = connection.execute("""
        SELECT sha256, pdf_path, thumbnail_path
        FROM documents
    """).fetchall()

    for row in rows:
        pdf_path = Path(row["pdf_path"])
        thumbnail_path = (
            Path(row["thumbnail_path"])
            if row["thumbnail_path"]
            else thumbnail_path_for(pdf_path)
        )
        try:
            create_thumbnail(pdf_path, thumbnail_path)
            if row["thumbnail_path"] != str(thumbnail_path):
                connection.execute("""
                    UPDATE documents
                    SET thumbnail_path = ?
                    WHERE sha256 = ?
                """, (str(thumbnail_path), row["sha256"]))
                connection.commit()
        except Exception:
            logging.exception("サムネイル失敗: %s", pdf_path)


def process_ocr(connection):
    rows = connection.execute("""
        SELECT * FROM documents
        WHERE status IN ('pending', 'error')
        ORDER BY original_modified
    """).fetchall()

    for row in rows:
        try:
            pdf_path = Path(row["pdf_path"])
            txt_path = Path(row["txt_path"])
            thumbnail_path = (
                Path(row["thumbnail_path"])
                if row["thumbnail_path"]
                else thumbnail_path_for(pdf_path)
            )
            create_thumbnail(pdf_path, thumbnail_path)

            with pymupdf.open(pdf_path) as document:
                if document.needs_pass:
                    raise RuntimeError("パスワード付きPDFです。")
                if document.page_count == 0:
                    raise RuntimeError("ページがありません。")

                page_count = (
                    document.page_count
                    if OCR_PAGES == -1
                    else min(OCR_PAGES, document.page_count)
                )
                page_bodies = []
                for page_number in range(page_count):
                    page = document[page_number]
                    text_page = page.get_textpage_ocr(
                        language=LANGUAGE,
                        dpi=DPI,
                        full=True,
                        tessdata=str(TESSDATA),
                    )
                    page_body = page.get_text(
                        "text", textpage=text_page, sort=True
                    ).strip()
                    page_bodies.append(normalize_ocr_text(page_body))
                body = "\n\n".join(page_bodies)
                target_description = (
                    "全ページ" if OCR_PAGES == -1
                    else f"先頭{page_count}ページ"
                )

            count = sum(not char.isspace() for char in body)
            status = "done" if count >= MIN_TEXT_CHARS else "review"
            processed_at = now_iso()

            text = (
                f"資料ID: {row['sha256']}\n"
                f"整理用日時: {row['original_modified']}\n"
                f"日時基準: コピー元の更新日時\n"
                f"元の作成日時: {row['original_created']}\n"
                f"取込日時: {row['imported_at']}\n"
                f"処理日時: {processed_at}\n"
                f"元ファイル: {row['original_path']}\n"
                f"保存PDF: {row['pdf_path']}\n"
                f"サムネイル: {thumbnail_path}\n"
                f"対象: {target_description}\n"
                f"OCR言語: {LANGUAGE}\n"
                f"状態: {status}\n\n"
                f"--- OCR本文 ---\n{body}\n"
            )

            temporary = WORK / f"{row['sha256']}.txt.part"
            try:
                temporary.write_text(text, encoding="utf-8")
                os.replace(temporary, txt_path)
            finally:
                temporary.unlink(missing_ok=True)

            connection.execute("""
                UPDATE documents
                SET status = ?, body = ?, error = NULL,
                    processed_at = ?, thumbnail_path = ?
                WHERE sha256 = ?
            """, (
                status,
                body,
                processed_at,
                str(thumbnail_path),
                row["sha256"],
            ))
            connection.commit()

            logging.info("OCR完了 [%s]: %s", status, pdf_path)

        except Exception as exc:
            connection.execute("""
                UPDATE documents
                SET status = 'error', error = ?
                WHERE sha256 = ?
            """, (str(exc), row["sha256"]))
            connection.commit()
            logging.exception("OCR失敗: %s", row["pdf_path"])


def search_documents(connection, keyword):
    # %や_をワイルドカードと解釈せず、文字列として検索
    escaped_path = (
        keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    normalized_keyword = normalize_ocr_text(keyword)
    escaped_body = (
        normalized_keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    path_pattern = f"%{escaped_path}%"
    body_pattern = f"%{escaped_body}%"

    rows = connection.execute("""
        SELECT original_modified, pdf_path, status, body
        FROM documents
        WHERE replace(replace(body, ' ', ''), '　', '')
                  LIKE ? ESCAPE '\\'
           OR original_path LIKE ? ESCAPE '\\'
        ORDER BY original_modified DESC
    """, (body_pattern, path_pattern)).fetchall()

    print(f"検索結果: {len(rows)}件")
    for row in rows:
        body = row["body"] or ""
        position = normalize_ocr_text(body).casefold().find(
            normalized_keyword.casefold()
        )
        start = max(0, position - 30)
        excerpt = body[start:start + 160].replace("\n", " ")

        print(f"\n日時: {row['original_modified']}")
        print(f"状態: {row['status']}")
        print(f"PDF: {row['pdf_path']}")
        print(f"抜粋: {excerpt}")


def create_webui():
    app = Flask(__name__)

    def query_documents(filters, untagged, limit=None):
        connection = open_database()
        try:
            conditions = []
            parameters = []
            if filters["text"]:
                conditions.append("""
                    (
                        pdf_path LIKE ?
                        OR original_path LIKE ?
                        OR replace(replace(body, ' ', ''), '　', '') LIKE ?
                        OR note LIKE ?
                    )
                """)
                pattern = f"%{filters['text']}%"
                parameters.extend((
                    pattern,
                    pattern,
                    f"%{normalize_ocr_text(filters['text'])}%",
                    pattern,
                ))
            if filters["from_date"]:
                conditions.append("date(original_modified) >= date(?)")
                parameters.append(filters["from_date"])
            if filters["to_date"]:
                conditions.append("date(original_modified) <= date(?)")
                parameters.append(filters["to_date"])
            if untagged:
                conditions.append("""
                    NOT EXISTS (
                        SELECT 1 FROM document_tags dt
                        WHERE dt.sha256 = d.sha256
                    )
                """)

            tag_terms = [
                term for term in filters["tag"].replace(",", " ").split()
                if term
            ]
            for term in tag_terms:
                conditions.append("""
                    EXISTS (
                        SELECT 1
                        FROM document_tags dt
                        JOIN tags t ON t.id = dt.tag_id
                        WHERE dt.sha256 = d.sha256 AND t.name LIKE ?
                    )
                """)
                parameters.append(f"%{term}%")

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            limit_clause = f"LIMIT {int(limit)}" if limit else ""
            return connection.execute(f"""
                SELECT d.*,
                       GROUP_CONCAT(t.name, ', ') AS tag_names,
                       COALESCE(NULLIF(d.pdf_path, ''), d.original_path)
                           AS filename_path
                FROM documents d
                LEFT JOIN document_tags dt ON dt.sha256 = d.sha256
                LEFT JOIN tags t ON t.id = dt.tag_id
                {where}
                GROUP BY d.sha256
                ORDER BY d.original_modified DESC
                {limit_clause}
            """, parameters).fetchall()
        finally:
            connection.close()

    @app.post("/upload")
    def upload_documents():
        uploaded_files = [
            uploaded_file
            for uploaded_file in request.files.getlist("files")
            if uploaded_file.filename
        ]
        if not uploaded_files:
            abort(400, description="PDFファイルを1件以上選択してください。")
        if any(
            Path(uploaded_file.filename).suffix.lower() != ".pdf"
            for uploaded_file in uploaded_files
        ):
            abort(400, description="PDFファイルのみ追加できます。")

        uploaded_at = time.time()
        imported_count = 0
        with single_instance():
            connection = open_database()
            try:
                for uploaded_file in uploaded_files:
                    upload_path = WORK / f"upload-{uuid.uuid4().hex}.pdf"
                    try:
                        uploaded_file.save(str(upload_path))
                        os.utime(upload_path, (uploaded_at, uploaded_at))
                        import_pdf(
                            connection,
                            upload_path,
                            fingerprint(upload_path),
                        )
                        imported_count += 1
                    finally:
                        upload_path.unlink(missing_ok=True)

                process_thumbnails(connection)
                process_ocr(connection)
            finally:
                connection.close()

        return redirect(url_for("webui", uploaded=imported_count))

    @app.get("/")
    def webui():
        filters = {
            key: request.args.get(key, "").strip()
            for key in ("text", "tag", "from_date", "to_date")
        }
        untagged = request.args.get("untagged") == "1"
        selected_tags = [
            tag.strip()
            for tag in request.args.getlist("selected_tag")
            if tag.strip()
        ]
        uploaded = request.args.get("uploaded", "")
        bulk_tag = request.args.get("bulk_tag", "").strip()
        if bulk_tag:
            selected_tags.extend(
                tag.strip() for tag in bulk_tag.replace(",", " ").split()
                if tag.strip()
            )
        selected_tags = list(dict.fromkeys(selected_tags))
        connection = open_database()
        try:
            priority_tags = [
                row["name"] for row in connection.execute("""
                    SELECT name FROM tags
                    WHERE is_hidden = 0 AND is_priority = 1
                    ORDER BY name COLLATE NOCASE
                """)
            ]
        finally:
            connection.close()
        limit = 50 if not any(filters.values()) and not untagged else None
        documents = query_documents(filters, untagged, limit=limit)
        documents = [
            {
                **{column: document[column] for column in document.keys()},
                "filename": Path(document["filename_path"]).name,
                "display_filename": "_".join(
                    Path(document["filename_path"]).stem.split("_")[:2]
                ),
            }
            for document in documents
        ]
        return render_template(
            "index.html",
            documents=documents,
            filters=filters,
            untagged=untagged,
            bulk_tag=",".join(selected_tags),
            selected_tags=selected_tags,
            priority_tags=priority_tags,
            uploaded=uploaded,
        )

    @app.get("/timeline")
    def timeline():
        today = datetime.now().date()
        month_value = request.args.get("month", "")
        try:
            month_date = datetime.strptime(month_value, "%Y-%m").date()
        except ValueError:
            month_date = today.replace(day=1)

        month_start = month_date.replace(day=1)
        month_end = month_start.replace(
            day=calendar.monthrange(month_start.year, month_start.month)[1]
        )
        calendar_weeks = calendar.Calendar(
            firstweekday=0
        ).monthdatescalendar(month_start.year, month_start.month)
        calendar_start = calendar_weeks[0][0]
        calendar_end = calendar_weeks[-1][-1]
        connection = open_database()
        try:
            counts = {
                row["day"]: row["count"]
                for row in connection.execute("""
                    SELECT date(original_modified) AS day, COUNT(*) AS count
                    FROM documents
                    WHERE date(original_modified) BETWEEN date(?) AND date(?)
                    GROUP BY date(original_modified)
                """, (calendar_start.isoformat(), calendar_end.isoformat()))
            }
        finally:
            connection.close()

        selected_date = request.args.get("date", "")
        period = request.args.get("period", "week")
        if period not in {"month", "week", "day"}:
            abort(400, description="期間の指定が不正です。")
        documents = []
        if selected_date:
            try:
                selected_day = datetime.strptime(
                    selected_date, "%Y-%m-%d"
                ).date()
            except ValueError:
                abort(400, description="日付の形式が不正です。")
            if period == "week":
                range_start = selected_day
                range_end = selected_day + timedelta(days=6)
            elif period == "month":
                range_start = selected_day
                next_month = (
                    range_start.replace(day=28) + timedelta(days=4)
                ).replace(day=1)
                target_day = min(
                    range_start.day,
                    calendar.monthrange(
                        next_month.year, next_month.month
                    )[1],
                )
                range_end = next_month.replace(day=target_day) - timedelta(
                    days=1
                )
            else:
                range_start = range_end = selected_day
            documents = query_documents({
                "text": "",
                "tag": "",
                "from_date": range_start.isoformat(),
                "to_date": range_end.isoformat(),
            }, False)
            documents = [
                {
                    **{column: document[column] for column in document.keys()},
                    "filename": Path(document["filename_path"]).name,
                    "display_filename": "_".join(
                        Path(document["filename_path"]).stem.split("_")[:2]
                    ),
                }
                for document in documents
            ]
            documents.sort(key=lambda document: document["original_modified"])

        previous_month = (month_start.replace(day=1) - timedelta(days=1))
        next_month = (
            month_end.replace(day=1) + timedelta(days=32)
        ).replace(day=1)
        return render_template(
            "timeline.html",
            month=month_start,
            weeks=calendar_weeks,
            counts=counts,
            selected_date=selected_date,
            period=period,
            documents=documents,
            previous_month=previous_month.strftime("%Y-%m"),
            next_month=next_month.strftime("%Y-%m"),
        )

    @app.get("/documents/<sha256>/edit")
    def edit_document(sha256):
        connection = open_database()
        try:
            document = connection.execute("""
                SELECT d.*, GROUP_CONCAT(t.name, ', ') AS tag_names
                FROM documents d
                LEFT JOIN document_tags dt ON dt.sha256 = d.sha256
                LEFT JOIN tags t ON t.id = dt.tag_id
                WHERE d.sha256 = ?
                GROUP BY d.sha256
            """, (sha256,)).fetchone()
            priority_tags = [
                row["name"] for row in connection.execute("""
                    SELECT name FROM tags
                    WHERE is_hidden = 0 AND is_priority = 1
                    ORDER BY name COLLATE NOCASE
                """)
            ]
        finally:
            connection.close()
        if document is None:
            abort(404)
        return render_template(
            "edit_document.html",
            document=document,
            priority_tags=priority_tags,
        )

    @app.post("/documents/<sha256>/update")
    def update_document(sha256):
        note = request.form.get("note", "").strip()
        raw_tags = request.form.get("tags", "")
        tag_names = {
            tag.strip() for tag in raw_tags.replace(",", " ").split()
            if tag.strip()
        }
        connection = open_database()
        try:
            if not connection.execute(
                "SELECT 1 FROM documents WHERE sha256 = ?", (sha256,)
            ).fetchone():
                abort(404)
            connection.execute(
                "UPDATE documents SET note = ? WHERE sha256 = ?",
                (note, sha256),
            )
            connection.execute(
                "DELETE FROM document_tags WHERE sha256 = ?", (sha256,)
            )
            for name in sorted(tag_names):
                connection.execute(
                    "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,)
                )
                tag_id = connection.execute(
                    "SELECT id FROM tags WHERE name = ?", (name,)
                ).fetchone()["id"]
                connection.execute(
                    "INSERT INTO document_tags (sha256, tag_id) VALUES (?, ?)",
                    (sha256, tag_id),
                )
            connection.commit()
        finally:
            connection.close()
        if request.form.get("untagged") == "1":
            return redirect(url_for("webui", untagged="1"))
        return redirect(url_for("webui"))

    @app.post("/documents/bulk-update")
    def bulk_update_documents():
        sha256s = list(dict.fromkeys(request.form.getlist("sha256")))
        if not sha256s:
            abort(400, description="一括更新するファイルを1件以上選択してください。")

        note = request.form.get("note", "").strip()
        raw_tags = request.form.get("tags", "").strip()
        append_tags = request.form.get("append_tags") == "1"
        tag_names = {
            tag.strip() for tag in raw_tags.replace(",", " ").split()
            if tag.strip()
        }
        if not note and not raw_tags:
            abort(400, description="メモまたはタグを入力してください。")

        connection = open_database()
        try:
            placeholders = ",".join("?" for _ in sha256s)
            existing = {
                row["sha256"]
                for row in connection.execute(
                    f"SELECT sha256 FROM documents WHERE sha256 IN ({placeholders})",
                    sha256s,
                )
            }
            if existing != set(sha256s):
                abort(404)

            if note:
                connection.executemany(
                    "UPDATE documents SET note = ? WHERE sha256 = ?",
                    [(note, sha256) for sha256 in sha256s],
                )

            if raw_tags:
                for name in sorted(tag_names):
                    connection.execute(
                        "INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,)
                    )

                tag_ids = {
                    name: connection.execute(
                        "SELECT id FROM tags WHERE name = ?", (name,)
                    ).fetchone()["id"]
                    for name in tag_names
                }
                for sha256 in sha256s:
                    if not append_tags:
                        connection.execute(
                            "DELETE FROM document_tags WHERE sha256 = ?",
                            (sha256,),
                        )
                    connection.executemany(
                        "INSERT OR IGNORE INTO document_tags "
                        "(sha256, tag_id) VALUES (?, ?)",
                        [(sha256, tag_ids[name]) for name in sorted(tag_names)],
                    )
            connection.commit()
        finally:
            connection.close()
        return redirect(url_for("webui"))

    @app.get("/tags")
    def tag_list():
        connection = open_database()
        try:
            tags = connection.execute("""
                SELECT t.id, t.name, t.is_priority,
                       COUNT(dt.sha256) AS document_count
                FROM tags t
                LEFT JOIN document_tags dt ON dt.tag_id = t.id
                WHERE t.is_hidden = 0
                GROUP BY t.id, t.name
                ORDER BY t.name COLLATE NOCASE
            """).fetchall()
        finally:
            connection.close()
        return render_template("tags.html", tags=tags)

    @app.post("/tags/<int:tag_id>/hide")
    def hide_tag(tag_id):
        connection = open_database()
        try:
            cursor = connection.execute(
                "UPDATE tags SET is_hidden = 1 WHERE id = ?",
                (tag_id,),
            )
            if cursor.rowcount == 0:
                abort(404)
            connection.commit()
        finally:
            connection.close()
        return redirect(url_for("tag_list"))

    @app.post("/tags/<int:tag_id>/priority")
    def toggle_tag_priority(tag_id):
        connection = open_database()
        try:
            cursor = connection.execute("""
                UPDATE tags
                SET is_priority = CASE is_priority WHEN 1 THEN 0 ELSE 1 END
                WHERE id = ? AND is_hidden = 0
            """, (tag_id,))
            if cursor.rowcount == 0:
                abort(404)
            connection.commit()
        finally:
            connection.close()
        return redirect(url_for("tag_list"))

    @app.get("/documents/<sha256>/<kind>")
    def document_file(sha256, kind):
        if kind not in {"pdf", "thumbnail", "txt"}:
            abort(404)
        connection = open_database()
        try:
            row = connection.execute(
                "SELECT pdf_path, thumbnail_path, txt_path "
                "FROM documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            abort(404)
        column = {
            "pdf": "pdf_path",
            "thumbnail": "thumbnail_path",
            "txt": "txt_path",
        }[kind]
        path = Path(row[column])
        if not path.is_file():
            abort(404)
        return send_file(path)

    return app


def run_webui(host, port):
    create_webui().run(host=host, port=port, debug=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", metavar="KEYWORD")
    parser.add_argument("--webui", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    SYSTEM.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(
                SYSTEM / "archive.log", encoding="utf-8"
            ),
            logging.StreamHandler(),
        ],
    )

    if args.webui:
        run_webui(args.host, args.port)
        return

    with single_instance():
        connection = open_database()
        try:
            if args.search is not None:
                search_documents(connection, args.search)
                return

            for language in LANGUAGE.split("+"):
                data = TESSDATA / f"{language}.traineddata"
                if not data.is_file():
                    raise FileNotFoundError(
                        f"OCR言語データがありません: {data}"
                    )
            if OCR_PAGES == 0 or OCR_PAGES < -1:
                raise ValueError(
                    "OCR_PAGESは1以上の値、または全ページを示す-1を指定してください。"
                )

            if SOURCE.resolve() == DEST.resolve():
                raise ValueError("コピー元と保存先は分けてください。")

            if SOURCE.is_dir():
                candidates = collect_candidates()
                if candidates:
                    time.sleep(STABLE_SECONDS)

                for source, expected in candidates:
                    try:
                        import_pdf(connection, source, expected)
                    except Exception:
                        logging.exception("取り込み失敗: %s", source)
            else:
                logging.warning(
                    "コピー元がありません。保存済みPDFのOCRのみ実行: %s",
                    SOURCE,
                )

            # コピー元が消えていても、保存済みの未完了PDFは処理する
            process_thumbnails(connection)
            process_ocr(connection)

            counts = connection.execute("""
                SELECT status, COUNT(*) AS count
                FROM documents
                GROUP BY status
            """).fetchall()

            for row in counts:
                logging.info("%s: %s件", row["status"], row["count"])

        finally:
            connection.close()


if __name__ == "__main__":
    main()