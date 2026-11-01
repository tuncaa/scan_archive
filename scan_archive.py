from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
import argparse
import hashlib
import logging
import msvcrt
import os
import shutil
import sqlite3
import time

import pymupdf


# ---------- 設定：実際の環境に合わせて変更 ----------
SOURCE = Path(r"C:\scanner_output")  # スキャナーの保存先
DEST = Path(r"C:\scan")              # PDF・TXTの保管先

TESSDATA = Path(r"C:\Program Files\Tesseract-OCR\tessdata")
LANGUAGE = "jpn+eng"
DPI = 300

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
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS source_files (
            source_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            PRIMARY KEY (source_path, sha256)
        );
    """)
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

    if not existing:
        # Python 3.12以降ではWindowsの作成日時にst_birthtimeを使用
        created = getattr(stat, "st_birthtime", stat.st_ctime)

        connection.execute("""
            INSERT INTO documents (
                sha256, pdf_path, txt_path, original_path,
                original_created, original_modified, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            digest,
            str(pdf_path),
            str(txt_path),
            str(source.resolve()),
            timestamp_iso(created),
            timestamp_iso(stat.st_mtime),
            now_iso(),
        ))

    elif not txt_path.exists():
        connection.execute("""
            UPDATE documents
            SET status = 'pending', error = NULL
            WHERE sha256 = ?
        """, (digest,))

    connection.execute("""
        INSERT INTO source_files (source_path, sha256, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(source_path, sha256)
        DO UPDATE SET last_seen = excluded.last_seen
    """, (str(source.resolve()), digest, now_iso()))

    connection.commit()


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

            with pymupdf.open(pdf_path) as document:
                if document.needs_pass:
                    raise RuntimeError("パスワード付きPDFです。")
                if document.page_count == 0:
                    raise RuntimeError("ページがありません。")

                page = document[0]
                text_page = page.get_textpage_ocr(
                    language=LANGUAGE,
                    dpi=DPI,
                    full=True,
                    tessdata=str(TESSDATA),
                )
                body = page.get_text(
                    "text", textpage=text_page, sort=True
                ).strip()

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
                f"対象: 1ページ目のみ\n"
                f"OCR言語: {LANGUAGE}\n"
                f"状態: {status}\n\n"
                f"--- 1ページ目の本文 ---\n{body}\n"
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
                    processed_at = ?
                WHERE sha256 = ?
            """, (status, body, processed_at, row["sha256"]))
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
    escaped = (
        keyword.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    pattern = f"%{escaped}%"

    rows = connection.execute("""
        SELECT original_modified, pdf_path, status, body
        FROM documents
        WHERE body LIKE ? ESCAPE '\\'
           OR original_path LIKE ? ESCAPE '\\'
        ORDER BY original_modified DESC
    """, (pattern, pattern)).fetchall()

    print(f"検索結果: {len(rows)}件")
    for row in rows:
        body = row["body"] or ""
        position = body.casefold().find(keyword.casefold())
        start = max(0, position - 30)
        excerpt = body[start:start + 160].replace("\n", " ")

        print(f"\n日時: {row['original_modified']}")
        print(f"状態: {row['status']}")
        print(f"PDF: {row['pdf_path']}")
        print(f"抜粋: {excerpt}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", metavar="KEYWORD")
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