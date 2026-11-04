"""SQLite持久化、历史采集快照与不含登录资料的备份。"""
import json
import sqlite3
import uuid
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path

from .models import Backup, Product, SearchFilters, SelectionPatch, Settings, timestamp


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    TABLES = {
        "products": ("id", "data"),
        "observations": ("id", "product_id", "data"),
        "tasks": ("id", "data"),
        "task_items": ("task_id", "product_id", "observation_id", "qualified", "reasons"),
        "selections": ("product_id", "status", "notes", "tags", "created_at", "updated_at"),
        "presets": ("id", "name", "filters"),
        "kv": ("key", "value"),
    }

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "workbench.sqlite3"
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id), data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_items (task_id TEXT REFERENCES tasks(id), product_id TEXT REFERENCES products(id), observation_id TEXT REFERENCES observations(id), qualified INTEGER NOT NULL, reasons TEXT NOT NULL, PRIMARY KEY(task_id, product_id));
                CREATE TABLE IF NOT EXISTS selections (product_id TEXT PRIMARY KEY REFERENCES products(id), status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS presets (id TEXT PRIMARY KEY, name TEXT NOT NULL, filters TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def tasks(self):
        with self.connect() as conn:
            return [json.loads(r[0]) for r in conn.execute("SELECT data FROM tasks ORDER BY rowid DESC")]

    def task(self, task_id):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def put_task(self, task):
        task["updated_at"] = timestamp()
        with self.connect() as conn:
            conn.execute("INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (task["id"], dump(task)))

    def product(self, product_id):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM products WHERE id=?", (product_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def latest_filters_for_product(self, product_id):
        with self.connect() as conn:
            row = conn.execute("SELECT t.data FROM task_items i JOIN tasks t ON i.task_id=t.id WHERE i.product_id=? ORDER BY i.rowid DESC LIMIT 1", (product_id,)).fetchone()
        return json.loads(row[0])["filters"] if row else None

    def put_product(self, product: Product, task_id=None, reasons=None):
        data, observation_id = dump(product.model_dump()), str(uuid.uuid4())
        with self.connect() as conn:
            existing = conn.execute("SELECT data FROM products WHERE id=?", (product.id,)).fetchone()
            if not existing or datetime.fromisoformat(json.loads(existing[0])["observed_at"]) <= datetime.fromisoformat(product.observed_at):
                conn.execute("INSERT INTO products VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (product.id, data))
            conn.execute("INSERT INTO observations VALUES (?,?,?)", (observation_id, product.id, data))
            if task_id:
                conn.execute("INSERT INTO task_items VALUES (?,?,?,?,?) ON CONFLICT(task_id,product_id) DO UPDATE SET observation_id=excluded.observation_id,qualified=excluded.qualified,reasons=excluded.reasons", (task_id, product.id, observation_id, int(not reasons), dump(reasons or [])))

    def task_items(self, task_id, qualified_only=True):
        with self.connect() as conn:
            sql = "SELECT o.data, i.reasons, i.qualified FROM task_items i JOIN observations o ON i.observation_id=o.id WHERE i.task_id=?"
            if qualified_only:
                sql += " AND i.qualified=1"
            return [{"product": json.loads(r[0]), "reasons": json.loads(r[1]), "qualified": bool(r[2])} for r in conn.execute(sql, (task_id,))]

    def save_selections(self, ids):
        with self.connect() as conn:
            for product_id in ids:
                if not conn.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
                    raise ValueError("商品不存在，请先完成采集")
                qualified = conn.execute("SELECT t.data AS task_data, p.data AS product_data FROM task_items i JOIN observations o ON i.observation_id=o.id JOIN products p ON i.product_id=p.id JOIN tasks t ON i.task_id=t.id WHERE i.product_id=? AND i.qualified=1 AND o.data=p.data ORDER BY i.rowid DESC LIMIT 1", (product_id,)).fetchone()
                if not qualified:
                    raise ValueError("该商品尚未通过严格筛选，不能加入选品库")
                from .filtering import evaluate
                if evaluate(Product.model_validate_json(qualified["product_data"]), SearchFilters.model_validate(json.loads(qualified["task_data"])["filters"])):
                    raise ValueError("商品资料已过期或不再符合条件，请重新采集核实")
                conn.execute("INSERT OR IGNORE INTO selections VALUES (?,?,?,?,?,?)", (product_id, "待评估", "", "[]", timestamp(), timestamp()))

    def selections(self):
        with self.connect() as conn:
            records = []
            for r in conn.execute("SELECT s.*,p.data FROM selections s JOIN products p ON p.id=s.product_id ORDER BY s.created_at DESC"):
                check = conn.execute("SELECT i.reasons FROM task_items i JOIN observations o ON i.observation_id=o.id WHERE i.product_id=? AND o.data=? ORDER BY i.rowid DESC LIMIT 1", (r["product_id"], r["data"])).fetchone()
                record = dict(r)
                record.pop("data")
                records.append({**record, "tags": json.loads(r["tags"]), "product": json.loads(r["data"]), "latest_reasons": json.loads(check[0]) if check else []})
            return records

    def patch_selection(self, product_id, patch: SelectionPatch):
        with self.connect() as conn:
            changed = conn.execute("UPDATE selections SET status=?,notes=?,tags=?,updated_at=? WHERE product_id=?", (patch.status, patch.notes, dump(patch.tags), timestamp(), product_id)).rowcount
        return bool(changed)

    def settings(self):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
        return json.loads(row[0]) if row else Settings().model_dump()

    def put_kv(self, key, value):
        with self.connect() as conn:
            conn.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, dump(value)))

    def get_kv(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def competitions(self, source_task_id=None):
        with self.connect() as conn:
            jobs = [json.loads(row[0]) for row in conn.execute("SELECT value FROM kv WHERE key LIKE 'competition:%' ORDER BY rowid DESC")]
        return [job for job in jobs if source_task_id is None or job["source_task_id"] == source_task_id]

    def competition(self, job_id):
        return self.get_kv("competition:" + job_id)

    def put_competition(self, job):
        job["updated_at"] = timestamp()
        self.put_kv("competition:" + job["id"], job)

    def presets(self):
        with self.connect() as conn:
            return [{"id": r["id"], "name": r["name"], "filters": json.loads(r["filters"])} for r in conn.execute("SELECT * FROM presets ORDER BY rowid DESC")]

    def save_preset(self, name, filters):
        preset_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute("INSERT INTO presets VALUES (?,?,?)", (preset_id, name, dump(filters)))
        return preset_id

    def delete_preset(self, preset_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM presets WHERE id=?", (preset_id,))

    def backup(self):
        with self.connect() as conn:
            tables = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in self.TABLES}
        # 接入验收与浏览器会话不随备份迁移。
        tables["kv"] = [r for r in tables["kv"] if r["key"] == "settings" or r["key"].startswith("competition:")]
        return {"format": "commerce-workbench", "version": 1, "exported_at": timestamp(), "tables": tables}

    def restore(self, backup: Backup):
        tables = backup.tables
        if set(tables) != set(self.TABLES):
            raise ValueError("备份表结构不完整")
        if sum(len(rows) for rows in tables.values()) > 100000:
            raise ValueError("备份记录过多")
        for table, rows in tables.items():
            for row in rows:
                if set(row) != set(self.TABLES[table]):
                    raise ValueError("备份字段不匹配")
                if table in ("products", "observations"):
                    product = Product.model_validate_json(row["data"])
                    if product.id != row["id" if table == "products" else "product_id"]:
                        raise ValueError("备份商品标识不一致")
                elif table == "selections":
                    SelectionPatch(status=row["status"], notes=row["notes"], tags=json.loads(row["tags"]))
                elif table == "presets":
                    SearchFilters.model_validate_json(row["filters"])
                elif table == "tasks":
                    task = json.loads(row["data"])
                    if task["id"] != row["id"]:
                        raise ValueError("备份任务标识不一致")
                    SearchFilters.model_validate(task["filters"])
                    task["status"] = "cancelled"
                    task["message"] = "从备份恢复的历史任务；如需采集请重试"
                    row["data"] = dump(task)
                elif table == "task_items":
                    if row["qualified"] not in (0, 1) or not isinstance(json.loads(row["reasons"]), list):
                        raise ValueError("备份筛选记录不正确")
                elif table == "kv":
                    if row["key"].startswith("competition:"):
                        from .competition_models import CompetitionJob
                        job = CompetitionJob.model_validate_json(row["value"])
                        if row["key"] != "competition:" + job.id:
                            raise ValueError("备份竞品任务标识不一致")
                        if job.status in {"queued", "running", "paused"}:
                            job.status = "interrupted"
                            job.message = "从备份恢复的竞品记录；请重新检索"
                        row["value"] = dump(job.model_dump())
                    elif row["key"] == "settings":
                        Settings.model_validate_json(row["value"])
                    else:
                        raise ValueError("备份包含不支持的设置")
        # 先完成验证，再在同一个事务中替换；任何失败均完整回滚。
        with self.connect() as conn:
            for table in reversed(self.TABLES):
                conn.execute(f"DELETE FROM {table}")
            for table, columns in self.TABLES.items():
                for row in tables[table]:
                    conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row[c] for c in columns))
