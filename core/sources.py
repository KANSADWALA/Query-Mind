"""Data sources: a catalog of real public datasets and a registry of the user's saved database connections.

Both are plain JSON files so new databases can be added from the UI *or* by editing the file:
  * datasets.json     — downloadable datasets (SQLite files or CSV/Excel files turned into SQLite)
  * connections.json  — saved connections to any SQLAlchemy-supported database (MySQL, PostgreSQL, ...)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from .config import CATALOG_FILE, CONNECTIONS_FILE, DATASETS_DIR

SQLITE_MAGIC = b"SQLite format 3\x00"
Progress = Callable[[float], None]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "dataset"


def table_name(text: str) -> str:
    name = slugify(Path(text).stem if "." in text else text)
    return f"t_{name}" if name[0].isdigit() else name


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def detect_kind(url_or_name: str) -> str:
    path = url_or_name.lower().split("?")[0]
    if path.endswith((".csv", ".tsv", ".txt")):
        return "csv"
    if path.endswith((".xlsx", ".xls")):
        return "excel"
    if path.endswith((".db", ".sqlite", ".sqlite3")):
        return "sqlite"
    return ""


def download(url: str, dest: Path, progress: Progress | None = None, timeout: int = 60) -> Path:
    """Stream *url* to *dest* (atomic: writes to .part then renames). Local paths and file:// are copied."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    if not re.match(r"^[a-z]+://", url) and Path(url).exists():
        shutil.copyfile(url, tmp)
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "QueryMind/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(min(done / total, 1.0))
    tmp.replace(dest)
    return dest


def read_table_file(path: Path, kind: str) -> dict[str, pd.DataFrame]:
    """Read a CSV/TSV (one table) or Excel workbook (one table per sheet)."""
    if kind == "excel":
        return {sheet: df for sheet, df in pd.read_excel(path, sheet_name=None).items()}
    sep = "\t" if path.suffix.lower() == ".tsv" else None
    return {path.stem: pd.read_csv(path, sep=sep, engine="python" if sep is None else "c")}


def frames_to_sqlite(frames: dict[str, pd.DataFrame], db_path: Path, replace_db: bool = True) -> Path:
    """Write dataframes as tables of a SQLite file, cleaning table and column names."""
    from sqlalchemy import create_engine

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if replace_db and db_path.exists():
        db_path.unlink()
    engine = create_engine(sqlite_url(db_path))
    with engine.begin() as conn:
        for raw, df in frames.items():
            df = df.copy()
            seen: dict[str, int] = {}
            cols = []
            for i, c in enumerate(df.columns):
                name = slugify(str(c)) if str(c).strip() else f"col{i}"
                if name in seen:
                    seen[name] += 1
                    name = f"{name}_{seen[name]}"
                seen.setdefault(name, 0)
                cols.append(name)
            df.columns = cols
            df.to_sql(table_name(raw), conn, if_exists="replace", index=False)
    engine.dispose()
    return db_path


# =============================================================== datasets
@dataclass
class Dataset:
    id: str
    name: str
    kind: str  # sqlite | csv | excel
    description: str = ""
    url: str = ""
    file: str = ""
    files: list[dict] = field(default_factory=list)  # [{"url": ..., "table": ...}] for csv/excel
    source: str = ""
    license: str = ""
    examples: list[str] = field(default_factory=list)
    custom: bool = False

    def db_path(self, folder: Path = DATASETS_DIR) -> Path:
        return folder / (self.file or f"{self.id}.sqlite") if self.kind == "sqlite" else folder / f"{self.id}.db"

    def is_ready(self, folder: Path = DATASETS_DIR) -> bool:
        return self.db_path(folder).exists()

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in ("", [], False) or k in ("id", "name", "kind")}


class DatasetCatalog:
    def __init__(self, path: Path = CATALOG_FILE, folder: Path = DATASETS_DIR):
        self.path, self.folder = path, folder

    def _read(self) -> dict:
        if not self.path.exists():
            return {"datasets": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def all(self) -> list[Dataset]:
        fields = set(Dataset.__dataclass_fields__)
        return [Dataset(**{k: v for k, v in d.items() if k in fields}) for d in self._read().get("datasets", [])]

    def get(self, dataset_id: str) -> Dataset | None:
        return next((d for d in self.all() if d.id == dataset_id), None)

    def ensure(self, dataset_id: str, progress: Progress | None = None, force: bool = False) -> str:
        """Download/convert the dataset if needed and return its SQLAlchemy URL."""
        ds = self.get(dataset_id)
        if ds is None:
            raise KeyError(f"Unknown dataset '{dataset_id}'")
        target = ds.db_path(self.folder)
        if target.exists() and not force:
            return sqlite_url(target)

        if ds.kind == "sqlite":
            download(ds.url, target, progress)
            with open(target, "rb") as fh:
                if fh.read(16) != SQLITE_MAGIC:
                    target.unlink()
                    raise ValueError(f"The file at {ds.url} is not a SQLite database.")
            return sqlite_url(target)

        raw_dir = self.folder / "raw" / ds.id
        frames: dict[str, pd.DataFrame] = {}
        files = ds.files or [{"url": ds.url, "table": ds.id}]
        for i, f in enumerate(files):
            fname = Path(f["url"].split("?")[0]).name or f"file{i}"
            local = download(f["url"], raw_dir / fname, progress)
            kind = detect_kind(fname) or ds.kind
            sheets = read_table_file(local, kind)
            for sheet, df in sheets.items():
                name = f.get("table") or sheet
                if len(sheets) > 1:  # multi-sheet workbook: one table per sheet
                    name = f"{f.get('table', '')}_{sheet}".strip("_")
                frames[name] = df
        frames_to_sqlite(frames, target)
        return sqlite_url(target)

    def add(self, name: str, url: str, kind: str = "", table: str = "", description: str = "",
            progress: Progress | None = None) -> Dataset:
        """Register a dataset from a URL (or local path), download it, and save it to the catalog."""
        kind = kind or detect_kind(url)
        if kind not in ("sqlite", "csv", "excel"):
            raise ValueError("Could not tell the file type. Use a URL ending in .csv, .xlsx or .db/.sqlite, or pick the type.")
        existing = {d.id for d in self.all()}
        base = slugify(name)
        ds_id, n = base, 2
        while ds_id in existing:
            ds_id, n = f"{base}_{n}", n + 1
        ds = Dataset(id=ds_id, name=name, kind=kind, description=description or f"Added from {url}", custom=True)
        if kind == "sqlite":
            ds.url, ds.file = url, f"{ds_id}.sqlite"
        else:
            ds.files = [{"url": url, "table": table_name(table or name)}]
        data = self._read()
        data.setdefault("datasets", []).append(ds.to_dict())
        self._write(data)
        try:
            self.ensure(ds_id, progress)
        except Exception:
            self.remove(ds_id)
            raise
        return ds

    def remove(self, dataset_id: str, delete_files: bool = True) -> None:
        ds = self.get(dataset_id)
        data = self._read()
        data["datasets"] = [d for d in data.get("datasets", []) if d.get("id") != dataset_id]
        self._write(data)
        if ds and delete_files:
            ds.db_path(self.folder).unlink(missing_ok=True)
            shutil.rmtree(self.folder / "raw" / dataset_id, ignore_errors=True)


# ============================================================ connections
@dataclass
class SavedConnection:
    name: str
    url: str  # SQLAlchemy URL; ${ENV_VAR} placeholders are expanded at connect time
    kind: str = ""  # mysql | postgresql | sqlite | upload | url
    description: str = ""
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def resolved_url(self) -> str:
        return os.path.expandvars(self.url)

    def masked_url(self) -> str:
        return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1•••@", self.url)


class ConnectionStore:
    def __init__(self, path: Path = CONNECTIONS_FILE):
        self.path = path

    def all(self) -> list[SavedConnection]:
        if not self.path.exists():
            return []
        try:
            items = json.loads(self.path.read_text(encoding="utf-8")).get("connections", [])
        except json.JSONDecodeError:
            return []
        fields = set(SavedConnection.__dataclass_fields__)
        return [SavedConnection(**{k: v for k, v in c.items() if k in fields}) for c in items]

    def _write(self, items: list[SavedConnection]) -> None:
        self.path.write_text(json.dumps({"connections": [asdict(c) for c in items]}, indent=2) + "\n", encoding="utf-8")

    def get(self, name: str) -> SavedConnection | None:
        return next((c for c in self.all() if c.name == name), None)

    def save(self, conn: SavedConnection) -> SavedConnection:
        items = [c for c in self.all() if c.name != conn.name]
        items.append(conn)
        self._write(items)
        return conn

    def remove(self, name: str) -> None:
        self._write([c for c in self.all() if c.name != name])


def kind_from_url(url: str) -> str:
    scheme = url.split(":", 1)[0].split("+", 1)[0].lower()
    return {"postgres": "postgresql", "mariadb": "mysql"}.get(scheme, scheme)
