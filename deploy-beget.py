#!/usr/bin/env python3
"""Загрузка сайта на Beget по FTP.

Доступы берутся из файла .beget.env рядом со скриптом (см. .beget.env.example).
Пароль нигде не печатается.

    python3 deploy-beget.py            # залить изменившиеся файлы
    python3 deploy-beget.py --all      # залить всё заново
    python3 deploy-beget.py --dry-run  # показать план, ничего не отправляя
"""

import ftplib
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".beget.env"

# Выгружаем только то, что действительно является частью сайта: белый список
# надёжнее чёрного — случайная заметка с паролями на хостинг не попадёт.
SKIP_NAMES = {".git", "node_modules", "__pycache__"}
ALLOWED_SUFFIX = {
    ".html", ".css", ".js", ".xml", ".json",
    ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico",
    ".woff", ".woff2", ".ttf", ".pdf",
}
ALLOWED_NAMES = {"robots.txt", ".htaccess"}
# тексты лицензий на шрифты — OFL требует распространять их вместе со шрифтами
ALLOWED_GLOBS = ("OFL*.txt",)


class ReusingFTP_TLS(ftplib.FTP_TLS):
    """FTPS с переиспользованием TLS-сессии на канале данных.

    Beget (vsftpd с require_ssl_reuse) иначе отвечает
    «522 SSL connection failed: session reuse required».
    """

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        return conn, size


def load_env(required=True):
    if not ENV.exists():
        if not required:
            return {}
        sys.exit(
            "Нет файла .beget.env — скопируйте .beget.env.example и заполните "
            "данными из письма Beget."
        )
    env = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    missing = [k for k in ("FTP_HOST", "FTP_USER", "FTP_PASS", "FTP_DIR") if not env.get(k)]
    if missing and required:
        sys.exit("В .beget.env не заполнено: " + ", ".join(missing))
    return env


def local_files():
    files = []
    for path in sorted(ROOT.rglob("*")):
        if any(part in SKIP_NAMES for part in path.relative_to(ROOT).parts):
            continue
        if path.is_dir():
            continue
        if path.name in ALLOWED_NAMES or any(path.match(g) for g in ALLOWED_GLOBS):
            files.append(path)
            continue
        if path.name.startswith(".") or path.suffix.lower() not in ALLOWED_SUFFIX:
            continue
        files.append(path)
    return files


def ensure_dir(ftp, remote_dir):
    """Создаёт каталог на сервере, если его нет."""
    parts = [p for p in remote_dir.split("/") if p]
    ftp.cwd("/")
    for part in parts:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)


def digest(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


def main():
    force = "--all" in sys.argv
    dry = "--dry-run" in sys.argv
    env = load_env(required=not dry)
    base = env.get("FTP_DIR", "<FTP_DIR>").rstrip("/")

    files = local_files()
    print(f"Файлов к выгрузке: {len(files)}")
    if dry:
        for f in files:
            print("  ", f.relative_to(ROOT))
        print(f"\n(--dry-run) Ушли бы в {env.get('FTP_HOST', '<FTP_HOST>')}:{base}")
        return

    # локальный кэш «что уже залито», чтобы не гонять неизменившееся
    state_file = ROOT / ".beget-state"
    state = {}
    if state_file.exists() and not force:
        for line in state_file.read_text(encoding="utf-8").splitlines():
            if " " in line:
                h, name = line.split(" ", 1)
                state[name] = h

    # Beget требует FTPS (explicit TLS); на обычный FTP откатываемся,
    # если сервер вдруг шифрование не поддерживает.
    try:
        ftp = ReusingFTP_TLS(env["FTP_HOST"], timeout=30)
        ftp.login(env["FTP_USER"], env["FTP_PASS"])
        ftp.prot_p()  # шифровать и канал данных
        mode = "FTPS"
    except ftplib.error_perm:
        ftp = ftplib.FTP(env["FTP_HOST"], timeout=30)
        ftp.login(env["FTP_USER"], env["FTP_PASS"])
        mode = "FTP"
    ftp.set_pasv(True)
    print(f"Подключились к {env['FTP_HOST']} как {env['FTP_USER']} ({mode})")

    uploaded = skipped = 0
    new_state = {}
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        h = digest(path)
        new_state[rel] = h
        if state.get(rel) == h:
            skipped += 1
            continue
        remote_dir = base + "/" + os.path.dirname(rel) if os.path.dirname(rel) else base
        ensure_dir(ftp, remote_dir)
        name = os.path.basename(rel)
        with path.open("rb") as fh:
            ftp.storbinary(f"STOR {name}", fh)
        # права по умолчанию бывают 600 — веб-серверу нужно 644
        try:
            ftp.sendcmd(f"SITE CHMOD 644 {name}")
        except ftplib.all_errors:
            pass
        print("  →", rel)
        uploaded += 1

    ftp.quit()
    state_file.write_text(
        "\n".join(f"{h} {name}" for name, h in sorted(new_state.items())), encoding="utf-8"
    )
    print(f"\nГотово: залито {uploaded}, без изменений {skipped}.")


if __name__ == "__main__":
    main()
