"""端到端测试：预览校验、整批替换、乐观并发冲突、幂等确认、失败保留旧批。"""
import io
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app  # noqa: E402

DAY = "2026-11-14"

VALID_CSV = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,12.3\n"
    "2026-11-14 01:00,15.6\n"
    "2026-11-14 02:00,11.0\n"
)

CSV_A = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,1.1\n"
    "2026-11-14 01:00,2.2\n"
)

CSV_B = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,9.9\n"
    "2026-11-14 01:00,8.8\n"
    "2026-11-14 02:00,7.7\n"
    "2026-11-14 03:00,6.6\n"
)

INVALID_CSV = (
    "采样时间,浓度\n"
    "2026-11-14 00:00,12.3\n"
    "2026-11-14 01:00,15.0\n"
    "2026-11-14 01:00,8.8\n"       # 时刻重复（与上一行同刻）
    "2026-11-14 02:00,-5.0\n"      # 负数
    "2026-11-15 03:00,9.9\n"       # 不在所选日期
    "2026-11-14 04:00,NaN\n"       # 非有限数
    "2026-11-14 05:00,inf\n"       # 非有限数
    "2026-11-14 06:00,abc\n"       # 非数字
)


@pytest.fixture()
def app(tmp_path):
    return create_app(db_path=str(tmp_path / "test.db"), secret_key="test")


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, csv_text, station_id=1, day=DAY):
    """上传 CSV，返回预览 token。"""
    resp = client.post(
        "/preview",
        data={"station_id": str(station_id), "day": day,
              "file": (io.BytesIO(csv_text.encode("utf-8")), "data.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302, resp.data[:500]
    location = resp.headers["Location"]
    assert location.startswith("/preview/")
    return location.rsplit("/", 1)[1]


def confirm(client, token):
    return client.post(f"/confirm/{token}")


def browse(client, station_id=1, day=DAY):
    return client.get(f"/browse?station_id={station_id}&day={day}")


# --------------------------------------------------------------------------- #

def test_valid_upload_preview_confirm_browse(client):
    token = upload(client, VALID_CSV)
    page = client.get(f"/preview/{token}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "有效读数" in html and "3 条" in html
    assert "确认发布" in html  # 无错误时出现确认按钮

    resp = confirm(client, token)
    assert resp.status_code == 302
    assert "/browse" in resp.headers["Location"]

    html = browse(client).get_data(as_text=True)
    assert "v1" in html
    assert "12.3" in html and "15.6" in html and "11.0" in html


def test_invalid_rows_block_publish(client):
    token = upload(client, INVALID_CSV)
    html = client.get(f"/preview/{token}").get_data(as_text=True)
    assert "整份文件不可发布" in html
    assert "浓度为负数" in html
    assert "采样时间不在所选日期" in html
    assert "重复" in html
    assert "非有限数" in html or "NaN" in html
    assert "浓度不是数字" in html
    assert "确认发布" not in html  # 有错误时不出确认按钮

    # 即便绕过页面直接 POST 确认，也会被拒绝，且浏览页仍无数据
    resp = confirm(client, token)
    assert resp.status_code == 302
    assert f"/preview/{token}" in resp.headers["Location"]
    assert "暂无已发布数据" in browse(client).get_data(as_text=True)


def test_replace_is_whole_batch(client):
    confirm(client, upload(client, CSV_A))          # v1：2 条
    confirm(client, upload(client, CSV_B))          # v2：4 条，整批替换
    html = browse(client).get_data(as_text=True)
    assert "v2" in html
    assert "9.9" in html and "6.6" in html          # 新批完整可见
    assert "1.1" not in html and "2.2" not in html  # 旧批整体消失
    assert "共 4 条" in html


def test_conflict_first_confirm_wins(client):
    confirm(client, upload(client, VALID_CSV))      # v1
    tok_a = upload(client, CSV_A)                   # 两份预览都基于 v1
    tok_b = upload(client, CSV_B)

    assert confirm(client, tok_a).status_code == 302   # 先确认者生效 → v2

    resp_b = confirm(client, tok_b)                    # 后确认者冲突
    assert resp_b.status_code == 409
    assert "冲突" in resp_b.get_data(as_text=True)

    # 冲突后再次确认同一份失效预览，依然不能发布
    assert confirm(client, tok_b).status_code == 409

    html = browse(client).get_data(as_text=True)       # 浏览页是 A 的数据
    assert "v2" in html and "1.1" in html and "9.9" not in html


def test_double_confirm_is_idempotent(client):
    confirm(client, upload(client, VALID_CSV))      # v1
    token = upload(client, CSV_A)
    assert confirm(client, token).status_code == 302   # → v2

    resp = confirm(client, token)                      # 重复确认同一预览
    assert resp.status_code == 302                     # 不报错、不换版
    html = browse(client).get_data(as_text=True)
    assert "v2" in html and "v3" not in html
    assert "1.1" in html


def test_failed_publish_keeps_old_batch(client):
    confirm(client, upload(client, VALID_CSV))      # v1
    # 制造一次必然失败的“确认”：带错误行的预览
    bad_token = upload(client, INVALID_CSV)
    confirm(client, bad_token)
    html = browse(client).get_data(as_text=True)
    assert "v1" in html and "12.3" in html          # 旧批原样保留

    # 冲突场景同样保留旧批
    tok_a = upload(client, CSV_A)
    tok_b = upload(client, CSV_B)
    confirm(client, tok_a)                          # v2
    assert confirm(client, tok_b).status_code == 409
    html = browse(client).get_data(as_text=True)
    assert "v2" in html and "1.1" in html


def test_concurrent_confirms_same_base_exactly_one_wins(app):
    c1, c2 = app.test_client(), app.test_client()
    confirm(c1, upload(c1, VALID_CSV))              # v1
    tok_a = upload(c1, CSV_A)
    tok_b = upload(c2, CSV_B)

    results = {}

    def worker(cl, tok, name):
        results[name] = cl.post(f"/confirm/{tok}").status_code

    t1 = threading.Thread(target=worker, args=(c1, tok_a, "a"))
    t2 = threading.Thread(target=worker, args=(c2, tok_b, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == [302, 409]   # 恰好一个生效、一个冲突
    html = browse(c1).get_data(as_text=True)
    assert "v2" in html
    # 浏览页只会是某一个完整批次，不会混杂
    assert ("1.1" in html) != ("9.9" in html)


def test_concurrent_double_confirm_same_preview_swaps_once(app):
    c1, c2 = app.test_client(), app.test_client()
    token = upload(c1, CSV_A)

    results = []

    def worker(cl):
        results.append(cl.post(f"/confirm/{token}").status_code)

    t1 = threading.Thread(target=worker, args=(c1,))
    t2 = threading.Thread(target=worker, args=(c2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert results == [302, 302]                    # 两次都“成功”
    html = browse(c1).get_data(as_text=True)
    assert "v1" in html and "v2" not in html        # 但只换版一次


def test_first_publish_race_on_empty_day(app):
    """无旧批（base_version 为 NULL）时并发确认，同样只有一个生效。"""
    c1, c2 = app.test_client(), app.test_client()
    tok_a = upload(c1, CSV_A)
    tok_b = upload(c2, CSV_B)

    results = {}

    def worker(cl, tok, name):
        results[name] = cl.post(f"/confirm/{tok}").status_code

    t1 = threading.Thread(target=worker, args=(c1, tok_a, "a"))
    t2 = threading.Thread(target=worker, args=(c2, tok_b, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results.values()) == [302, 409]
    assert "v1" in browse(c1).get_data(as_text=True)


def test_english_headers_and_iso_time(client):
    csv_text = (
        "timestamp,pm2.5\n"
        "2026-11-14T08:00:00,33.3\n"
        "2026-11-14T09:00:00,0\n"     # 0 是合法浓度
    )
    token = upload(client, csv_text)
    assert confirm(client, token).status_code == 302
    html = browse(client).get_data(as_text=True)
    assert "33.3" in html and "0.0" in html
