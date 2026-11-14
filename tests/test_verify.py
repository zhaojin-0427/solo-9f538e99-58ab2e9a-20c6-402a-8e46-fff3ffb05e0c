"""封存与核验测试：SHA-256 链式封存、篡改检出、旧版不补封、快照一致性。"""
import io
import os
import re
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app, seal_digest  # noqa: E402

DAY = "2026-11-14"

CSV_V1 = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,12.3\n"
    "2026-11-14 01:00,15.6\n"
    "2026-11-14 02:00,11.0\n"
)
CSV_V2 = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,20.0\n"
    "2026-11-14 01:00,21.1\n"
)
CSV_V3 = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,30.0\n"
    "2026-11-14 01:00,31.1\n"
    "2026-11-14 02:00,32.2\n"
    "2026-11-14 03:00,33.3\n"
)
INVALID_CSV = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,12.3\n"
    "2026-11-14 01:00,-5.0\n"   # 负数：整份不可发布
)

HEX64 = re.compile(r"\b[0-9a-f]{64}\b")


@pytest.fixture()
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "test.db"), secret_key="test")


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, csv_text, station_id=1, day=DAY):
    resp = client.post(
        "/preview",
        data={"station_id": str(station_id), "day": day,
              "file": (io.BytesIO(csv_text.encode("utf-8")), "data.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302, resp.data[:500]
    return resp.headers["Location"].rsplit("/", 1)[1]


def confirm(client, token):
    return client.post(f"/confirm/{token}")


def publish(client, csv_text, station_id=1, day=DAY):
    resp = confirm(client, upload(client, csv_text, station_id, day))
    assert resp.status_code == 302
    return resp


def verify_page(client, station_id=1, day=DAY):
    resp = client.get(f"/verify?station_id={station_id}&day={day}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def db_conn(app):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def seals(app):
    with db_conn(app) as conn:
        return conn.execute(
            "SELECT s.batch_id, s.prev_digest, s.digest, b.version"
            " FROM seals s JOIN batches b ON b.id = s.batch_id"
            " ORDER BY b.version").fetchall()


# --------------------------------------------------------------------------- #
# 封存写入
# --------------------------------------------------------------------------- #

def test_publish_writes_chained_seals(app, client):
    publish(client, CSV_V1)                     # v1：链起点
    publish(client, CSV_V2)                     # v2：串接 v1
    rows = seals(app)
    assert len(rows) == 2
    assert rows[0]["version"] == 1 and rows[0]["prev_digest"] is None
    assert rows[1]["version"] == 2
    assert rows[1]["prev_digest"] == rows[0]["digest"]      # 串接前一已封存版本
    for r in rows:
        assert HEX64.fullmatch(r["digest"])

    # 摘要内容与本地重算一致（站点、日期、版本、前驱、逐时读数）
    expected_v1 = seal_digest("城东监测站", DAY, 1, None, [
        ("2026-11-14 00:00:00", 12.3),
        ("2026-11-14 01:00:00", 15.6),
        ("2026-11-14 02:00:00", 11.0),
    ])
    assert rows[0]["digest"] == expected_v1


def test_no_seal_on_conflict_reconfirm_or_failure(app, client):
    publish(client, CSV_V1)                     # v1
    tok_a = upload(client, CSV_V2)
    tok_b = upload(client, CSV_V3)              # 两份预览同基于 v1
    assert confirm(client, tok_a).status_code == 302   # → v2，新增一条封存
    assert len(seals(app)) == 2

    assert confirm(client, tok_b).status_code == 409   # 冲突：不新增封存
    assert len(seals(app)) == 2

    assert confirm(client, tok_a).status_code == 302   # 重复确认：幂等，不新增封存
    assert len(seals(app)) == 2

    bad = upload(client, INVALID_CSV)                  # 含错误行：发布被拒
    assert confirm(client, bad).status_code == 302
    assert len(seals(app)) == 2                        # 失败不新增封存


# --------------------------------------------------------------------------- #
# 核验结论
# --------------------------------------------------------------------------- #

def test_verify_page_all_valid(client):
    publish(client, CSV_V1)
    publish(client, CSV_V2)
    html = verify_page(client)
    assert "核验通过" in html
    assert "2 个封存版本" in html
    assert html.count("封存有效") == 2
    assert "首个失效版本" not in html and "不可信" not in html
    assert len(HEX64.findall(html)) >= 3   # 两枚摘要 + 一条前驱


def test_tampered_value_detected_and_chain_untrusted(app, client):
    publish(client, CSV_V1)
    publish(client, CSV_V2)
    with db_conn(app) as conn:              # 离线维护：改 v1 的一条读数
        conn.execute("UPDATE readings SET concentration=999.9"
                     " WHERE sampled_at='2026-11-14 01:00:00'"
                     " AND batch_id=(SELECT id FROM batches WHERE version=1)")
        conn.commit()
    html = verify_page(client)
    assert "核验未通过" in html
    assert "首个失效版本 <b>v1</b>" in html   # 首个失效版本是 v1
    assert "摘要与本地重算不符" in html
    assert "不可信" in html                   # v2 随之不可信
    assert "封存有效" not in html


def test_missing_row_detected(app, client):
    publish(client, CSV_V1)
    with db_conn(app) as conn:              # 离线维护：删掉一行读数
        conn.execute("DELETE FROM readings"
                     " WHERE sampled_at='2026-11-14 02:00:00'")
        conn.commit()
    html = verify_page(client)
    assert "首个失效版本 <b>v1</b>" in html
    assert "摘要与本地重算不符" in html


def test_reordered_rows_not_an_anomaly(app, client):
    publish(client, CSV_V1)
    publish(client, CSV_V2)
    with db_conn(app) as conn:
        # 离线维护只改变了行的物理存储顺序（内容原样重排）
        for (batch_id,) in conn.execute("SELECT id FROM batches").fetchall():
            rows = conn.execute(
                "SELECT sampled_at, concentration FROM readings"
                " WHERE batch_id=?", (batch_id,)).fetchall()
            conn.execute("DELETE FROM readings WHERE batch_id=?", (batch_id,))
            conn.executemany(
                "INSERT INTO readings(batch_id, sampled_at, concentration)"
                " VALUES (?,?,?)",
                [(batch_id, r["sampled_at"], r["concentration"])
                 for r in reversed(rows)])
        conn.commit()
    html = verify_page(client)
    assert "核验通过" in html                 # 读取顺序变化不算异常
    assert "首个失效版本" not in html


def test_deleted_middle_version_breaks_chain(app, client):
    publish(client, CSV_V1)
    publish(client, CSV_V2)
    publish(client, CSV_V3)
    with db_conn(app) as conn:              # 离线维护：删掉中间的 v2
        bid = conn.execute("SELECT id FROM batches WHERE version=2"
                           ).fetchone()["id"]
        conn.execute("DELETE FROM seals WHERE batch_id=?", (bid,))
        conn.execute("DELETE FROM readings WHERE batch_id=?", (bid,))
        conn.execute("DELETE FROM batches WHERE id=?", (bid,))
        conn.commit()
    html = verify_page(client)
    assert "首个失效版本 <b>v3</b>" in html   # v3 的前驱指向已不存在的 v2
    assert "链断裂" in html
    assert "封存有效" in html                 # v1 仍有效


def test_tampered_prev_pointer_detected(app, client):
    publish(client, CSV_V1)
    publish(client, CSV_V2)
    with db_conn(app) as conn:              # 改动 v2 的前驱指针
        conn.execute("UPDATE seals SET prev_digest='0'*64"
                     " WHERE batch_id=(SELECT id FROM batches WHERE version=2)")
        conn.commit()
    html = verify_page(client)
    assert "首个失效版本 <b>v2</b>" in html
    assert "链断裂" in html


def test_tampered_version_metadata_detected(app, client):
    publish(client, CSV_V1)
    with db_conn(app) as conn:              # 改动批次元数据（版本号）
        conn.execute("UPDATE batches SET version=7 WHERE version=1")
        conn.commit()
    html = verify_page(client)
    assert "首个失效版本 <b>v7</b>" in html
    assert "摘要与本地重算不符" in html


# --------------------------------------------------------------------------- #
# 旧版未封存，不补封
# --------------------------------------------------------------------------- #

def test_legacy_unsealed_version_not_backfilled(app, client):
    with db_conn(app) as conn:
        # 模拟封存机制启用前发布的旧版 v1（无 seals 行）
        bid = conn.execute(
            "INSERT INTO batches(station_id, day, version, created_at)"
            " VALUES (1, ?, 1, '2026-11-01 10:00:00')", (DAY,)).lastrowid
        conn.executemany(
            "INSERT INTO readings(batch_id, sampled_at, concentration)"
            " VALUES (?,?,?)",
            [(bid, "2026-11-14 00:00:00", 9.9),
             (bid, "2026-11-14 01:00:00", 8.8)])
        conn.execute(
            "INSERT INTO current_batch(station_id, day, batch_id, version)"
            " VALUES (1, ?, ?, 1)", (DAY, bid))
        conn.commit()

    publish(client, CSV_V2)                 # 下一次发布 → v2，新链从此开始
    rows = seals(app)
    assert len(rows) == 1                   # 旧版不被补封
    assert rows[0]["version"] == 2 and rows[0]["prev_digest"] is None

    html = verify_page(client)
    assert "未封存" in html                 # v1 标为未封存
    assert "封存有效" in html               # v2 有效
    assert "核验通过" in html               # 链本身完整
    assert "首个失效版本" not in html


# --------------------------------------------------------------------------- #
# 快照一致性：补发与核验交错
# --------------------------------------------------------------------------- #

def test_verify_consistent_under_concurrent_republish(app, client):
    publish(client, CSV_V1)
    stop = threading.Event()
    failures = []

    def publisher():
        c = app.test_client()
        for _ in range(6):
            publish(c, CSV_V2)

    def verifier():
        c = app.test_client()
        while not stop.is_set():
            html = verify_page(c)
            # 快照一致 ⇒ 链必然自洽；读到半个批次才会出现误报
            if "首个失效版本" in html or "不可信" in html:
                failures.append(html)

    threads = [threading.Thread(target=publisher)] + [
        threading.Thread(target=verifier) for _ in range(3)]
    for t in threads:
        t.start()
    threads[0].join()
    stop.set()
    for t in threads[1:]:
        t.join()

    assert failures == []
    html = verify_page(client)              # 全部发布完成后链仍完整
    assert "核验通过" in html
    assert "7 个封存版本" in html
