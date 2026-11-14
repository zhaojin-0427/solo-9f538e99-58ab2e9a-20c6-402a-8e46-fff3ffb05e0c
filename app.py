#!/usr/bin/env python3
"""空气监测站 PM2.5 逐时数据发布平台（Flask + SQLite）。

一致性设计：
- 确认发布在单个 SQLite 事务（BEGIN IMMEDIATE）内完成：浏览页只会看到
  完整旧批或完整新批，不会看到半个批次；
- 每条预览记录基准版本 base_version（预览生成时已发布批次的版本号，
  NULL 表示当时无已发布数据）。确认时比对当前版本，实现乐观并发控制：
  基于同一旧批的两次预览，先确认者生效，后确认者收到冲突并需重新预览；
- 同一预览重复确认是幂等的：状态已为 confirmed 时直接跳转，不再次换版；
- 发布过程中任何异常都会回滚事务，旧批数据原样保留。

封存与核验：
- 每次成功发布在同一事务内为该批次写入 SHA-256 封存摘要（站点、日期、
  版本、逐时读数），并以 prev_digest 串接同站同日前一已封存版本，构成
  链式封存；失败、冲突、重复确认均不会新增封存；
- 核验页按站点、日期列出全部发布版本，在单个读事务快照内逐版本重算
  摘要并校验链式衔接：首个失效版本之后的封存链标为不可信；读数的存储
  顺序不影响摘要，改值或缺行会被检出；
- 封存机制启用前发布的旧版没有摘要，标为“未封存”，不参与链，也不补封。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import secrets
import sqlite3
from datetime import date, datetime

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "pm25.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- 每一次成功发布产生一个不可变批次；读数挂在批次上
CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    day        TEXT NOT NULL,               -- YYYY-MM-DD
    version    INTEGER NOT NULL,            -- 每个 (station_id, day) 从 1 递增
    created_at TEXT NOT NULL,
    UNIQUE (station_id, day, version)
);

-- 当前已发布批次指针：确认时在事务内整体切换，浏览端只见完整旧批或完整新批
CREATE TABLE IF NOT EXISTS current_batch (
    station_id INTEGER NOT NULL,
    day        TEXT NOT NULL,
    batch_id   INTEGER NOT NULL REFERENCES batches(id),
    version    INTEGER NOT NULL,
    PRIMARY KEY (station_id, day)
);

CREATE TABLE IF NOT EXISTS readings (
    batch_id      INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    sampled_at    TEXT NOT NULL,            -- YYYY-MM-DD HH:MM:SS（已归一化）
    concentration REAL NOT NULL,
    PRIMARY KEY (batch_id, sampled_at)
);

CREATE TABLE IF NOT EXISTS previews (
    token        TEXT PRIMARY KEY,
    station_id   INTEGER NOT NULL REFERENCES stations(id),
    day          TEXT NOT NULL,
    base_version INTEGER,                   -- 预览时已发布批次版本；NULL=当时无数据
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'confirmed', 'stale')),
    rows_json    TEXT NOT NULL,
    errors_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 发布批次的 SHA-256 封存摘要：与确认发布同一事务写入。
-- prev_digest 串接同站同日前一已封存版本，构成链式封存；
-- 封存机制启用前的旧批次没有对应行（未封存），且不会补封。
CREATE TABLE IF NOT EXISTS seals (
    batch_id    INTEGER PRIMARY KEY REFERENCES batches(id),
    prev_digest TEXT,                       -- 前一已封存版本的 digest；NULL=链起点
    digest      TEXT NOT NULL
);
"""

DEFAULT_STATIONS = ("城东监测站", "城西监测站", "滨江监测站", "高新监测站")

MAX_FILE_BYTES = 2 * 1024 * 1024  # 上传 CSV 大小上限

# 表头别名（比较前会去掉空格/下划线/连字符并转小写）
TIME_ALIASES = {"sampledat", "timestamp", "time", "datetime", "采样时间", "时间"}
CONC_ALIASES = {"concentration", "pm25", "pm2.5", "value",
                "浓度", "pm25浓度", "pm2.5浓度"}

FALLBACK_TIME_FORMATS = ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")


# --------------------------------------------------------------------------- #
# 解析与校验
# --------------------------------------------------------------------------- #

def _norm_header(cell: str) -> str:
    return (cell or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def parse_time(raw: str):
    """解析采样时间，返回 naive datetime；无法解析返回 None。"""
    s = (raw or "").strip()
    if not s:
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in FALLBACK_TIME_FORMATS:
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt.replace(tzinfo=None)


def parse_concentration(raw: str):
    """返回 (value, None) 或 (None, 错误原因)。"""
    s = (raw or "").strip()
    if not s:
        return None, "浓度为空"
    try:
        value = float(s)
    except ValueError:
        return None, "浓度不是数字"
    if not math.isfinite(value):
        return None, "浓度不是有限数（NaN/Inf）"
    if value < 0:
        return None, "浓度为负数"
    return value, None


def parse_csv(text: str, day: str):
    """解析 CSV 文本，返回 (有效读数列表, 错误行列表)。

    任一错误都会使整份文件不可发布（由调用方控制）。
    """
    rows, errors = [], []
    reader = csv.reader(io.StringIO(text))

    header = None
    for record in reader:
        if any(cell.strip() for cell in record):
            header = record
            break
    if header is None:
        errors.append({"line": "-", "content": "", "reason": "文件为空或缺少表头"})
        return rows, errors

    names = [_norm_header(c) for c in header]

    def find_col(aliases):
        for i, name in enumerate(names):
            if name in aliases:
                return i
        return None

    t_idx, c_idx = find_col(TIME_ALIASES), find_col(CONC_ALIASES)
    if t_idx is None or c_idx is None:
        errors.append({
            "line": 1,
            "content": ",".join(header)[:120],
            "reason": "表头缺少“采样时间”或“浓度”列",
        })
        return rows, errors

    need = max(t_idx, c_idx)
    seen = {}  # 归一化时刻 -> 首次出现的行号
    for record in reader:
        if not any(cell.strip() for cell in record):
            continue  # 跳过空行
        line = reader.line_num
        content = ",".join(record)[:120]
        if len(record) <= need:
            errors.append({"line": line, "content": content, "reason": "列数不足"})
            continue
        dt = parse_time(record[t_idx])
        if dt is None:
            errors.append({"line": line, "content": content, "reason": "采样时间无法解析"})
            continue
        ts = dt.strftime("%Y-%m-%d %H:%M:%S")
        if dt.date().isoformat() != day:
            errors.append({"line": line, "content": content, "reason": "采样时间不在所选日期"})
            continue
        if ts in seen:
            errors.append({"line": line, "content": content,
                           "reason": f"采样时刻与第 {seen[ts]} 行重复"})
            continue
        value, err = parse_concentration(record[c_idx])
        if err:
            errors.append({"line": line, "content": content, "reason": err})
            continue
        seen[ts] = line
        rows.append({"line": line, "sampled_at": ts, "concentration": value})

    if not rows and not errors:
        errors.append({"line": "-", "content": "", "reason": "文件不含任何数据行"})
    rows.sort(key=lambda r: r["sampled_at"])
    return rows, errors


# --------------------------------------------------------------------------- #
# 封存摘要与链式核验
# --------------------------------------------------------------------------- #

def seal_digest(station_name: str, day: str, version: int,
                prev_digest: str | None, readings) -> str:
    """计算批次封存摘要（SHA-256 十六进制）。

    摘要内容：站点名、日期、版本号、前驱摘要与全部逐时读数。
    readings 为 (sampled_at, concentration) 可迭代对象；读数先按采样时刻
    排序再入摘要，与存储/读取的物理顺序无关——离线维护若只改变行顺序
    不算异常，而改值、缺行或改动站点/日期/版本都会使摘要失配。
    """
    canon = sorted((ts, float(c)) for ts, c in readings)
    lines = [
        "pm25-seal-v1",
        f"station={station_name}",
        f"day={day}",
        f"version={version}",
        f"prev={prev_digest or '-'}",
        f"readings={len(canon)}",
    ]
    lines.extend(f"{ts}|{c!r}" for ts, c in canon)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def load_and_verify_chain(db: sqlite3.Connection, station, day: str):
    """读取该站该日全部发布版本并逐版本核验封存链。

    必须在调用方开启的事务内调用：版本列表、逐时读数与核验结论来自
    同一数据库快照，与补发交错时不会看到半个批次。

    返回按版本升序的 dict 列表，status 取值：
    - valid     封存有效（摘要与链式衔接均一致）
    - broken    首个失效版本（摘要失配或链断裂）
    - untrusted 位于失效版本之后的链上版本，不可信
    - unsealed  无摘要的旧版（封存机制启用前发布），不参与链
    """
    rows = db.execute(
        "SELECT b.id, b.version, b.created_at, s.prev_digest, s.digest"
        " FROM batches b LEFT JOIN seals s ON s.batch_id = b.id"
        " WHERE b.station_id=? AND b.day=? ORDER BY b.version",
        (station["id"], day)).fetchall()
    versions = []
    expected_prev = None   # 下一封存版本应声明的前驱摘要
    chain_broken = False
    for r in rows:
        readings = db.execute(
            "SELECT sampled_at, concentration FROM readings"
            " WHERE batch_id=? ORDER BY sampled_at",
            (r["id"],)).fetchall()
        v = {"version": r["version"], "created_at": r["created_at"],
             "readings": readings, "digest": r["digest"],
             "prev_digest": r["prev_digest"], "reason": None}
        if r["digest"] is None:
            # 封存机制启用前发布的旧版：无摘要可核验，不影响链
            v["status"] = "unsealed"
        elif chain_broken:
            v["status"] = "untrusted"
            v["reason"] = "链上存在更早的失效版本，本版本随之不可信"
        elif r["prev_digest"] != expected_prev:
            v["status"] = "broken"
            v["reason"] = ("前驱摘要与链上前一已封存版本不符：链断裂"
                           "（前序封存版本被删除或链接关系被改动）")
            chain_broken = True
        else:
            recomputed = seal_digest(
                station["name"], day, r["version"], r["prev_digest"],
                [(x["sampled_at"], x["concentration"]) for x in readings])
            if recomputed != r["digest"]:
                v["status"] = "broken"
                v["reason"] = ("封存摘要与本地重算不符：读数被改值/缺行，"
                               "或站点、日期、版本信息被改动")
                chain_broken = True
            else:
                v["status"] = "valid"
                expected_prev = r["digest"]
        versions.append(v)
    return versions


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #

def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")  # 读写不互斥，浏览不被发布阻塞
        if conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0] == 0:
            conn.executemany("INSERT INTO stations(name) VALUES (?)",
                             [(n,) for n in DEFAULT_STATIONS])
        conn.commit()
    finally:
        conn.close()


def create_app(db_path: str | None = None, secret_key: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    app.secret_key = secret_key or os.environ.get("SECRET_KEY") or secrets.token_hex(16)
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES

    init_db(app.config["DB_PATH"])

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            conn = sqlite3.connect(app.config["DB_PATH"],
                                   isolation_level=None,  # 手动控制事务
                                   timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            g.db = conn
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ------------------------------- 页面 ------------------------------- #

    @app.get("/")
    def index():
        stations = get_db().execute("SELECT * FROM stations ORDER BY id").fetchall()
        return render_template("index.html", stations=stations,
                               today=date.today().isoformat())

    @app.post("/preview")
    def create_preview():
        db = get_db()
        station_id = request.form.get("station_id", type=int)
        day = (request.form.get("day") or "").strip()
        station = db.execute("SELECT * FROM stations WHERE id=?",
                             (station_id,)).fetchone() if station_id else None
        if station is None:
            abort(400, "站点不存在")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            abort(400, "日期格式应为 YYYY-MM-DD")

        upload = request.files.get("file")
        if upload is None or upload.filename == "":
            flash("请选择要上传的 CSV 文件。", "error")
            return redirect(url_for("index"))

        raw = upload.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            rows, errors = [], [{"line": "-", "content": "",
                                 "reason": "文件编码不是有效的 UTF-8"}]
        else:
            rows, errors = parse_csv(text, day)

        # 记录基准版本：当前已发布批次的版本号（无则 NULL）
        cur = db.execute(
            "SELECT version FROM current_batch WHERE station_id=? AND day=?",
            (station_id, day)).fetchone()
        base_version = cur["version"] if cur else None

        token = secrets.token_urlsafe(16)
        db.execute(
            "INSERT INTO previews(token, station_id, day, base_version, status,"
            " rows_json, errors_json, created_at) VALUES (?,?,?,?, 'pending',?,?,?)",
            (token, station_id, day, base_version,
             json.dumps(rows, ensure_ascii=False),
             json.dumps(errors, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")))
        return redirect(url_for("show_preview", token=token))

    def load_preview(token: str):
        pv = get_db().execute("SELECT * FROM previews WHERE token=?",
                              (token,)).fetchone()
        if pv is None:
            abort(404)
        return pv

    @app.get("/preview/<token>")
    def show_preview(token):
        db = get_db()
        pv = load_preview(token)
        station = db.execute("SELECT * FROM stations WHERE id=?",
                             (pv["station_id"],)).fetchone()
        rows = json.loads(pv["rows_json"])
        errors = json.loads(pv["errors_json"])
        publishable = pv["status"] == "pending" and not errors and bool(rows)
        return render_template("preview.html", pv=pv, station=station,
                               rows=rows, errors=errors, publishable=publishable)

    @app.post("/confirm/<token>")
    def confirm(token):
        db = get_db()
        try:
            # BEGIN IMMEDIATE 立即取得写锁：并发确认在此串行化，
            # 版本检查与换版在同一事务内，杜绝两个确认同时生效。
            db.execute("BEGIN IMMEDIATE")
            pv = db.execute("SELECT * FROM previews WHERE token=?",
                            (token,)).fetchone()
            if pv is None:
                db.execute("ROLLBACK")
                abort(404)
            station = db.execute("SELECT * FROM stations WHERE id=?",
                                 (pv["station_id"],)).fetchone()
            browse_url = url_for("browse", station_id=pv["station_id"], day=pv["day"])

            # 幂等：同一预览重复确认，不再次换版
            if pv["status"] == "confirmed":
                db.execute("COMMIT")
                flash("该预览此前已确认过，本次操作未重复换版。", "info")
                return redirect(browse_url)
            if pv["status"] == "stale":
                db.execute("COMMIT")
                return render_template("conflict.html", station=station,
                                       day=pv["day"]), 409

            rows = json.loads(pv["rows_json"])
            errors = json.loads(pv["errors_json"])
            if errors or not rows:  # 含错误行的文件整份不可发布
                db.execute("COMMIT")
                flash("文件存在错误行，整份文件不可发布。", "error")
                return redirect(url_for("show_preview", token=token))

            cur = db.execute(
                "SELECT version FROM current_batch WHERE station_id=? AND day=?",
                (pv["station_id"], pv["day"])).fetchone()
            current_version = cur["version"] if cur else None
            if current_version != pv["base_version"]:
                # 基于同一旧批的另一份预览已先确认 → 冲突，需重新预览
                db.execute("UPDATE previews SET status='stale' WHERE token=?", (token,))
                db.execute("COMMIT")
                return render_template("conflict.html", station=station,
                                       day=pv["day"]), 409

            new_version = (current_version or 0) + 1
            now = datetime.now().isoformat(timespec="seconds")
            batch_id = db.execute(
                "INSERT INTO batches(station_id, day, version, created_at)"
                " VALUES (?,?,?,?)",
                (pv["station_id"], pv["day"], new_version, now)).lastrowid
            db.executemany(
                "INSERT INTO readings(batch_id, sampled_at, concentration)"
                " VALUES (?,?,?)",
                [(batch_id, r["sampled_at"], r["concentration"]) for r in rows])
            # 原子换版：指针切换与读数写入处于同一事务
            db.execute(
                "INSERT INTO current_batch(station_id, day, batch_id, version)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(station_id, day) DO UPDATE SET"
                " batch_id=excluded.batch_id, version=excluded.version",
                (pv["station_id"], pv["day"], batch_id, new_version))
            # 封存：与确认发布同一事务计算并写入 SHA-256 摘要，
            # prev_digest 串接同站同日前一已封存版本（无则 NULL，成为链起点）。
            # 失败回滚、冲突、重复确认的路径都在此前返回，不会新增封存。
            prev = db.execute(
                "SELECT s.digest FROM seals s"
                " JOIN batches b ON b.id = s.batch_id"
                " WHERE b.station_id=? AND b.day=?"
                " ORDER BY b.version DESC LIMIT 1",
                (pv["station_id"], pv["day"])).fetchone()
            prev_digest = prev["digest"] if prev else None
            digest = seal_digest(
                station["name"], pv["day"], new_version, prev_digest,
                [(r["sampled_at"], r["concentration"]) for r in rows])
            db.execute(
                "INSERT INTO seals(batch_id, prev_digest, digest) VALUES (?,?,?)",
                (batch_id, prev_digest, digest))
            db.execute("UPDATE previews SET status='confirmed' WHERE token=?", (token,))
            db.execute("COMMIT")
            flash(f"发布成功：{station['name']} {pv['day']} 已整批替换为 "
                  f"v{new_version}（{len(rows)} 条读数）。", "success")
            return redirect(browse_url)
        except Exception:
            try:
                db.execute("ROLLBACK")  # 发布失败：旧批原样保留
            except sqlite3.Error:
                pass
            raise

    @app.get("/browse")
    def browse():
        db = get_db()
        stations = db.execute("SELECT * FROM stations ORDER BY id").fetchall()
        station_id = request.args.get("station_id", type=int) \
            or (stations[0]["id"] if stations else None)
        day = request.args.get("day") or date.today().isoformat()
        station = batch = None
        readings = []
        if station_id:
            station = db.execute("SELECT * FROM stations WHERE id=?",
                                 (station_id,)).fetchone()
            batch = db.execute(
                "SELECT b.id, b.version, b.created_at FROM current_batch cb"
                " JOIN batches b ON b.id = cb.batch_id"
                " WHERE cb.station_id=? AND cb.day=?",
                (station_id, day)).fetchone()
            if batch:
                readings = db.execute(
                    "SELECT sampled_at, concentration FROM readings"
                    " WHERE batch_id=? ORDER BY sampled_at",
                    (batch["id"],)).fetchall()
        return render_template("browse.html", stations=stations, station=station,
                               day=day, batch=batch, readings=readings)

    @app.get("/verify")
    def verify():
        db = get_db()
        # 整个核验（版本列表、逐时读数、结论）在同一个读事务内完成：
        # WAL 快照保证与补发交错时看到的是某一时刻的完整链，
        # 不会因读到半个批次而误报。
        db.execute("BEGIN")
        try:
            stations = db.execute("SELECT * FROM stations ORDER BY id").fetchall()
            station_id = request.args.get("station_id", type=int) \
                or (stations[0]["id"] if stations else None)
            day = request.args.get("day") or date.today().isoformat()
            station, versions = None, []
            if station_id:
                station = db.execute("SELECT * FROM stations WHERE id=?",
                                     (station_id,)).fetchone()
                if station:
                    versions = load_and_verify_chain(db, station, day)
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        first_bad = next((v for v in versions if v["status"] == "broken"), None)
        sealed_count = sum(1 for v in versions if v["status"] != "unsealed")
        return render_template("verify.html", stations=stations, station=station,
                               day=day, versions=versions, first_bad=first_bad,
                               sealed_count=sealed_count)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True)
