"""
微信公众号对接模块
将此模块与 app.py 合并，或单独运行后配置到微信公众号后台

依赖：pip install wechatpy flask
"""

import os
import re
from datetime import datetime, timedelta
from flask import request, make_response
from wechatpy import parse_message, create_reply
from wechatpy.utils import check_signature
from wechatpy.exceptions import InvalidSignatureException

# 从你的微信公众号后台获取的配置
WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "your_wechat_token_here")
WECHAT_ADMIN_OPENID = os.getenv("WECHAT_ADMIN_OPENID", "")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "").strip()


def is_query_intent(text):
    keywords = ["查询", "查", "看看", "统计", "汇总", "分析", "花了", "消费", "支出", "收入", "多少", "构成", "占比"]
    return any(kw in text for kw in keywords)


def extract_year_month_query(text, now):
    match = re.search(r"(?:(\d{4})\s*年)?\s*(\d{1,2})\s*月", text)
    if not match:
        return None
    year = int(match.group(1)) if match.group(1) else now.year
    month = int(match.group(2))
    if month < 1 or month > 12:
        return None

    from datetime import datetime, timedelta
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = datetime(year, month + 1, 1) - timedelta(days=1)
    return start, end, f"{year}年{month}月"


def parse_compact_date(date_str, fallback_year):
    from datetime import datetime
    full = re.match(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|号)?", date_str)
    if full:
        return datetime(int(full.group(1)), int(full.group(2)), int(full.group(3)))

    md = re.match(r"(\d{1,2})[-/.月](\d{1,2})(?:日|号)?", date_str)
    if md:
        return datetime(fallback_year, int(md.group(1)), int(md.group(2)))
    return None


def extract_date_range_query(text, now):
    range_match = re.search(
        r"([\d年月日号./-]+)\s*(?:到|至|~|－|—|-)\s*([\d年月日号./-]+)",
        text,
    )
    if not range_match:
        return None

    left = parse_compact_date(range_match.group(1).strip(), now.year)
    right = parse_compact_date(range_match.group(2).strip(), now.year)
    if not left or not right:
        return None
    if left > right:
        left, right = right, left

    label = f"{left.month}月{left.day}日~{right.month}月{right.day}日"
    if left.year != now.year or right.year != now.year:
        label = f"{left.year}-{left.month:02d}-{left.day:02d} ~ {right.year}-{right.month:02d}-{right.day:02d}"
    return left, right, label


def build_period_summary_reply(conn, user_id, start_date, end_date, label):
    start_key = start_date.year * 10000 + start_date.month * 100 + start_date.day
    end_key = end_date.year * 10000 + end_date.month * 100 + end_date.day

    summary_row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN bill_type='expense' THEN amount ELSE 0 END) AS expense,
            SUM(CASE WHEN bill_type='income' THEN amount ELSE 0 END) AS income,
            COUNT(*) AS cnt
        FROM bills
        WHERE user_id=? AND (year*10000 + month*100 + day) BETWEEN ? AND ?
        """,
        (user_id, start_key, end_key),
    ).fetchone()

    cat_rows = conn.execute(
        """
        SELECT category, SUM(amount) AS total
        FROM bills
        WHERE user_id=?
          AND bill_type='expense'
          AND (year*10000 + month*100 + day) BETWEEN ? AND ?
        GROUP BY category
        ORDER BY total DESC
        """,
        (user_id, start_key, end_key),
    ).fetchall()

    expense = summary_row["expense"] or 0
    income = summary_row["income"] or 0
    count = summary_row["cnt"] or 0

    lines = [f"📊 {label} 统计"]
    lines.append(f"💸 总支出：¥{expense:.2f}")
    lines.append(f"💰 总收入：¥{income:.2f}")
    lines.append(f"💎 结余：¥{income - expense:.2f}")
    lines.append(f"📝 记录笔数：{count}笔")
    if cat_rows:
        lines.append("\n📌 支出构成：")
        for row in cat_rows[:6]:
            ratio = (row["total"] / expense * 100) if expense > 0 else 0
            lines.append(f"  {row['category']}：¥{row['total']:.2f}（{ratio:.1f}%）")
    else:
        lines.append("\n📌 支出构成：暂无支出记录")
    return "\n".join(lines)


def build_keyword_summary_reply(conn, user_id, keyword, start_date, end_date, label):
    start_key = start_date.year * 10000 + start_date.month * 100 + start_date.day
    end_key = end_date.year * 10000 + end_date.month * 100 + end_date.day
    keyword_like = f"%{keyword}%"
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN bill_type='expense' THEN amount ELSE 0 END) AS expense,
            SUM(CASE WHEN bill_type='income' THEN amount ELSE 0 END) AS income,
            COUNT(*) AS cnt
        FROM bills
        WHERE user_id=?
          AND (year*10000 + month*100 + day) BETWEEN ? AND ?
          AND (description LIKE ? OR category LIKE ?)
        """,
        (user_id, start_key, end_key, keyword_like, keyword_like),
    ).fetchone()

    expense = row["expense"] or 0
    income = row["income"] or 0
    cnt = row["cnt"] or 0
    lines = [f"🔎 关键词“{keyword}”在{label}的统计"]
    lines.append(f"📝 命中笔数：{cnt}笔")
    lines.append(f"💸 支出：¥{expense:.2f}")
    lines.append(f"💰 收入：¥{income:.2f}")
    lines.append(f"💎 净额：¥{income - expense:.2f}")
    return "\n".join(lines)


def build_week_trend_reply(conn, user_id, start_date, end_date, label):
    start_key = start_date.year * 10000 + start_date.month * 100 + start_date.day
    end_key = end_date.year * 10000 + end_date.month * 100 + end_date.day
    rows = conn.execute(
        """
        SELECT year, month, day,
               SUM(CASE WHEN bill_type='expense' THEN amount ELSE 0 END) AS expense,
               SUM(CASE WHEN bill_type='income' THEN amount ELSE 0 END) AS income
        FROM bills
        WHERE user_id=?
          AND (year*10000 + month*100 + day) BETWEEN ? AND ?
        GROUP BY year, month, day
        ORDER BY year, month, day
        """,
        (user_id, start_key, end_key),
    ).fetchall()

    daily_map = {(r["year"], r["month"], r["day"]): r for r in rows}
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    lines = [f"📈 {label} 每日趋势"]
    total_expense = 0
    total_income = 0
    best_day = (None, -1.0)

    cur = start_date
    while cur <= end_date:
        rec = daily_map.get((cur.year, cur.month, cur.day))
        exp = (rec["expense"] if rec else 0) or 0
        inc = (rec["income"] if rec else 0) or 0
        total_expense += exp
        total_income += inc
        if exp > best_day[1]:
            best_day = (cur, exp)

        wk = weekday_names[cur.weekday()]
        lines.append(f"{wk} {cur.month}/{cur.day}：支出¥{exp:.2f}，收入¥{inc:.2f}")
        cur += timedelta(days=1)

    lines.append(f"\n💸 合计支出：¥{total_expense:.2f}")
    lines.append(f"💰 合计收入：¥{total_income:.2f}")
    if best_day[0] is not None and best_day[1] > 0:
        lines.append(f"🔥 支出最高：{best_day[0].month}/{best_day[0].day}（¥{best_day[1]:.2f}）")
    return "\n".join(lines)

def register_wechat_routes(app, init_db_func, parse_bill_func, get_db_func, get_month_summary_func, format_summary_reply_func):
    """注册微信路由到 Flask app"""

    @app.route("/wechat", methods=["GET", "POST"])
    def wechat():
        signature = request.args.get("signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")

        # 验证服务器
        try:
            check_signature(WECHAT_TOKEN, signature, timestamp, nonce)
        except InvalidSignatureException:
            return "Invalid signature", 403

        if request.method == "GET":
            return echostr

        # 处理消息
        from datetime import datetime
        msg = parse_message(request.data)

        if msg.type == "text":
            content = msg.content.strip()
            reply_text = handle_message(
                content,
                msg.source,
                parse_bill_func,
                get_db_func,
                get_month_summary_func,
                format_summary_reply_func,
            )
        else:
            reply_text = "请发送文字消息来记账，例如：吃饭15块"

        reply = create_reply(reply_text, msg)
        return make_response(reply.render())


def get_user_record(conn, user_id):
    return conn.execute(
        "SELECT user_id, status, display_name, requested_note, requested_at, approved_at, approved_by FROM wechat_users WHERE user_id=?",
        (user_id,),
    ).fetchone()


def ensure_admin_record(conn, user_id):
    if not user_id:
        return
    conn.execute(
        """
        INSERT INTO wechat_users (user_id, status, requested_note, requested_at, approved_at, approved_by)
        VALUES (?, 'admin', 'system-admin', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'system')
        ON CONFLICT(user_id) DO UPDATE SET
            status='admin',
            approved_at=CURRENT_TIMESTAMP,
            approved_by='system'
        """,
        (user_id,),
    )


def bootstrap_first_admin(conn, user_id):
    admin_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM wechat_users WHERE status='admin'"
    ).fetchone()["cnt"]
    if admin_count:
        return False
    conn.execute(
        """
        INSERT INTO wechat_users (user_id, status, requested_note, requested_at, approved_at, approved_by)
        VALUES (?, 'admin', 'bootstrap-admin', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'bootstrap')
        ON CONFLICT(user_id) DO UPDATE SET
            status='admin',
            approved_at=CURRENT_TIMESTAMP,
            approved_by='bootstrap'
        """,
        (user_id,),
    )
    return True


def upsert_pending_user(conn, user_id, note, nickname="", contact_tail=""):
    conn.execute(
        """
        INSERT INTO wechat_users (user_id, status, display_name, apply_nickname, apply_contact_tail, requested_note, requested_at)
        VALUES (?, 'pending', '', ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            status='pending',
            apply_nickname=excluded.apply_nickname,
            apply_contact_tail=excluded.apply_contact_tail,
            requested_note=excluded.requested_note,
            requested_at=CURRENT_TIMESTAMP,
            approved_at=NULL,
            approved_by=NULL
        """,
        (user_id, nickname, contact_tail, note),
    )


def approve_user(conn, admin_user_id, target_user_id):
    record = get_user_record(conn, target_user_id)
    if record is None:
        return False
    conn.execute(
        "UPDATE wechat_users SET status='approved', approved_at=CURRENT_TIMESTAMP, approved_by=? WHERE user_id=?",
        (admin_user_id, target_user_id),
    )
    conn.execute(
        """
        INSERT INTO approval_logs (user_id, action, operator, channel, note, created_at)
        VALUES (?, 'approve', ?, 'wechat', '微信命令审批通过', CURRENT_TIMESTAMP)
        """,
        (target_user_id, admin_user_id),
    )
    return True


def reject_user(conn, admin_user_id, target_user_id):
    record = get_user_record(conn, target_user_id)
    if record is None:
        return False
    conn.execute(
        "UPDATE wechat_users SET status='rejected', approved_at=CURRENT_TIMESTAMP, approved_by=? WHERE user_id=?",
        (admin_user_id, target_user_id),
    )
    conn.execute(
        """
        INSERT INTO approval_logs (user_id, action, operator, channel, note, created_at)
        VALUES (?, 'reject', ?, 'wechat', '微信命令审批拒绝', CURRENT_TIMESTAMP)
        """,
        (target_user_id, admin_user_id),
    )
    return True


def list_pending_users(conn):
    return conn.execute(
        "SELECT user_id, display_name, apply_nickname, apply_contact_tail, requested_note, requested_at FROM wechat_users WHERE status='pending' ORDER BY requested_at ASC"
    ).fetchall()


def list_approved_users(conn):
    return conn.execute(
        "SELECT user_id, status, display_name, approved_at, approved_by FROM wechat_users WHERE status IN ('approved', 'admin') ORDER BY approved_at DESC"
    ).fetchall()


def format_pending_reply(rows):
    if not rows:
        return "当前没有待审批申请。"
    lines = ["待审批列表："]
    for row in rows[:10]:
        display_name = row["display_name"] or "未备注"
        apply_nickname = row["apply_nickname"] or "未填"
        contact_tail = row["apply_contact_tail"] or "未填"
        note = row["requested_note"] or "未填写"
        lines.append(f"- {display_name} | {row['user_id']} | 昵称：{apply_nickname} | 尾号：{contact_tail} | 申请：{note}")
    lines.append("发送：同意 openid")
    lines.append("发送：拒绝 openid")
    return "\n".join(lines)


def format_approved_reply(rows):
    if not rows:
        return "当前没有已批准用户。"
    lines = ["已批准用户："]
    for row in rows[:10]:
        suffix = " (管理员)" if row["status"] == "admin" else ""
        display_name = row["display_name"] or "未备注"
        lines.append(f"- {display_name} | {row['user_id']}{suffix}")
    return "\n".join(lines)


def extract_target_user_id(text):
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return parts[1].strip()


def parse_application_payload(text):
    raw = text.strip()
    if raw.startswith("申请使用"):
        content = raw.replace("申请使用", "", 1).strip()
    elif raw.startswith("申请"):
        content = raw.replace("申请", "", 1).strip()
    else:
        content = raw

    nickname_match = re.search(r"(?:昵称|name)\s*[：:=]\s*([^,，;；\s]+)", content, flags=re.IGNORECASE)
    tail_match = re.search(r"(?:手机尾号|尾号|手机号尾号|phone)\s*[：:=]\s*(\d{4})", content, flags=re.IGNORECASE)
    reason_match = re.search(r"(?:理由|备注|用途|reason)\s*[：:=]\s*(.+)$", content, flags=re.IGNORECASE)

    nickname = nickname_match.group(1).strip() if nickname_match else ""
    contact_tail = tail_match.group(1).strip() if tail_match else ""

    if reason_match:
        note = reason_match.group(1).strip()
    else:
        cleaned = content
        cleaned = re.sub(r"(?:昵称|name)\s*[：:=]\s*([^,，;；\s]+)", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?:手机尾号|尾号|手机号尾号|phone)\s*[：:=]\s*(\d{4})", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(?:理由|备注|用途|reason)\s*[：:=]\s*", "", cleaned, flags=re.IGNORECASE)
        note = re.sub(r"\s+", " ", cleaned).strip(" ,，;；")

    if not note:
        note = "微信申请接入"

    return nickname, contact_tail, note

def handle_message(text, user_id, parse_bill_func, get_db_func, get_month_summary_func, format_summary_reply_func):
    """处理微信消息"""
    conn = get_db_func()
    ensure_admin_record(conn, WECHAT_ADMIN_OPENID)
    bootstrapped = False
    if not WECHAT_ADMIN_OPENID:
        bootstrapped = bootstrap_first_admin(conn, user_id)
    conn.commit()

    if text in ["我的ID", "我的openid", "openid"]:
        suffix = "\n你当前是管理员。" if bootstrapped else ""
        conn.close()
        return f"你的 user_id 是：\n{user_id}{suffix}"

    record = get_user_record(conn, user_id)
    is_admin = (bool(WECHAT_ADMIN_OPENID) and user_id == WECHAT_ADMIN_OPENID) or (record is not None and record["status"] == "admin")
    is_approved = is_admin or (record is not None and record["status"] == "approved")

    if bootstrapped and text not in ["帮助", "help", "？", "?"]:
        conn.close()
        return (
            "你已被设为首个管理员。\n"
            f"你的 user_id 是：{user_id}\n"
            "现在可直接记账，也可发送“审批列表”查看申请。"
        )

    if is_admin:
        if text in ["审批列表", "待审批", "申请列表"]:
            pending_rows = list_pending_users(conn)
            conn.close()
            return format_pending_reply(pending_rows)
        if text in ["用户列表", "已批准用户"]:
            approved_rows = list_approved_users(conn)
            conn.close()
            return format_approved_reply(approved_rows)
        if text.startswith("同意 "):
            target_user_id = extract_target_user_id(text)
            ok = approve_user(conn, user_id, target_user_id)
            conn.commit()
            conn.close()
            if ok:
                return f"已批准用户：{target_user_id}"
            return "未找到该申请用户，请先让对方发送“申请使用”。"
        if text.startswith("拒绝 "):
            target_user_id = extract_target_user_id(text)
            ok = reject_user(conn, user_id, target_user_id)
            conn.commit()
            conn.close()
            if ok:
                return f"已拒绝用户：{target_user_id}"
            return "未找到该申请用户，请先让对方发送“申请使用”。"

    if not is_approved:
        if text.startswith("申请使用") or text == "申请":
            nickname, contact_tail, note = parse_application_payload(text)
            upsert_pending_user(conn, user_id, note, nickname, contact_tail)
            conn.commit()
            conn.close()
            return (
                "申请已提交，等待管理员审批。\n"
                "建议格式：申请使用 昵称:小李 尾号:1234 理由:家庭日常账本\n"
                "审批通过后，就能直接发送“吃饭10块”这类消息记账。"
            )

        status_text = record["status"] if record is not None else "未申请"
        conn.close()
        return (
            "当前你还没有使用权限。\n"
            f"当前状态：{status_text}\n"
            "发送“申请使用 昵称:xx 尾号:1234 理由:xxx”提交申请。\n"
            "发送“我的ID”可查看自己的 user_id。"
        )

    if text in {"撤销", "撤回", "撤销上一笔", "删除上一笔"}:
        latest = conn.execute(
            "SELECT id, amount, category, description, bill_type, created_at, year, month, day FROM bills WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not latest:
            conn.close()
            return "当前账本没有可撤销的记录。"

        conn.execute(
            """
            INSERT INTO undo_actions (user_id, amount, category, description, bill_type, source_created_at, year, month, day)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                latest["amount"],
                latest["category"],
                latest["description"],
                latest["bill_type"],
                latest["created_at"],
                latest["year"],
                latest["month"],
                latest["day"],
            ),
        )
        conn.execute("DELETE FROM bills WHERE id=? AND user_id=?", (latest["id"], user_id))
        conn.commit()
        conn.close()
        return f"🗑️ 已撤销上一笔：{latest['category']} ¥{latest['amount']:.2f}（{latest['description']}）\n可发送“恢复上一笔”找回。"

    if text in {"恢复", "恢复上一笔", "恢复删除", "找回上一笔"}:
        latest_undo = conn.execute(
            """
            SELECT id, amount, category, description, bill_type, source_created_at, year, month, day
            FROM undo_actions
            WHERE user_id=?
            ORDER BY deleted_at DESC, id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not latest_undo:
            conn.close()
            return "没有可恢复的记录。"

        conn.execute(
            "INSERT INTO bills (user_id, amount, category, description, bill_type, created_at, year, month, day) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                latest_undo["amount"],
                latest_undo["category"],
                latest_undo["description"],
                latest_undo["bill_type"],
                latest_undo["source_created_at"],
                latest_undo["year"],
                latest_undo["month"],
                latest_undo["day"],
            ),
        )
        conn.execute("DELETE FROM undo_actions WHERE id=?", (latest_undo["id"],))
        conn.commit()
        conn.close()
        return f"♻️ 已恢复上一笔：{latest_undo['category']} ¥{latest_undo['amount']:.2f}（{latest_undo['description']}）"

    # 查询指令
    if any(kw in text for kw in ["可视化", "图表", "趋势图", "仪表盘", "看图", "看报表", "看网页"]):
        link = f"{WEB_BASE_URL}/?user_id={user_id}" if WEB_BASE_URL else "（请联系管理员配置 WEB_BASE_URL 后可直接打开网页）"
        conn.close()
        return f"📈 可视化看板链接：\n{link}"

    now = datetime.now()
    if any(kw in text for kw in ["上周", "上星期"]):
        start_this_week = now - timedelta(days=now.weekday())
        start_last_week = start_this_week - timedelta(days=7)
        end_last_week = start_this_week - timedelta(days=1)
        reply = build_period_summary_reply(conn, user_id, start_last_week, end_last_week, "上周")
        conn.close()
        return reply

    if any(kw in text for kw in ["本周趋势", "本周每天", "本周走势", "本周消费趋势"]):
        start_this_week = now - timedelta(days=now.weekday())
        end_this_week = now
        reply = build_week_trend_reply(conn, user_id, start_this_week, end_this_week, "本周")
        conn.close()
        return reply

    date_range = extract_date_range_query(text, now)
    if date_range and (is_query_intent(text) or "到" in text or "至" in text):
        start_date, end_date, label = date_range
        reply = build_period_summary_reply(conn, user_id, start_date, end_date, label)
        conn.close()
        return reply

    ym_query = extract_year_month_query(text, now)
    if ym_query and (is_query_intent(text) or "月" in text):
        start_date, end_date, label = ym_query
        reply = build_period_summary_reply(conn, user_id, start_date, end_date, label)
        conn.close()
        return reply

    keyword_match = re.search(r"(?:查|搜索|统计|看看)\s*([\w\u4e00-\u9fff]{1,20})", text)
    if keyword_match and ("关键词" in text or not any(k in text for k in ["本月", "上月", "上周", "今天", "昨天"])):
        keyword = keyword_match.group(1).strip()
        if keyword and keyword not in {"账单", "消费", "支出", "收入", "趋势", "图表"}:
            start_date, end_date, label = (datetime(now.year, now.month, 1), now, "本月")
            ym = extract_year_month_query(text, now)
            if ym:
                start_date, end_date, label = ym
            reply = build_keyword_summary_reply(conn, user_id, keyword, start_date, end_date, label)
            conn.close()
            return reply

    if any(kw in text for kw in ["本月", "这月", "账单", "统计", "花了多少", "查询"]):
        summary = get_month_summary_func(now.year, now.month, user_id)
        conn.close()
        return format_summary_reply_func(summary, now.year, now.month)

    # 帮助指令
    if text in ["帮助", "help", "？", "?"]:
        conn.close()
        return (
            "📖 使用说明\n"
            "──────────\n"
            "记账：直接发送消费内容\n"
            "例如：吃饭15块\n"
            "      打车20元\n"
            "      买衣服299元\n"
            "      收到工资8000\n\n"
            "查询：发送「本月账单」\n"
            "扩展查询：发送「3月消费」「上周消费」「3月1日到3月15日消费」\n"
            "关键词：发送「查滴滴」「统计星巴克」\n"
            "趋势：发送「本周趋势」\n"
            "纠错：发送「撤销上一笔」「恢复上一笔」\n"
            "申请：发送「申请使用 昵称:xx 尾号:1234 理由:xxx」\n"
            "查看ID：发送「我的ID」"
        )

    # 解析记账
    result = parse_bill_func(text)
    if result is None:
        conn.close()
        return "没有识别到金额哦 🤔\n请重新输入，例如：吃饭10块、打车20元\n\n发送「帮助」查看使用说明"

    amount, category, description, bill_type = result
    now = datetime.now()

    conn.execute(
        "INSERT INTO bills (user_id, amount, category, description, bill_type, created_at, year, month, day) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, amount, category, description, bill_type, now.isoformat(), now.year, now.month, now.day)
    )
    conn.commit()
    conn.close()

    emoji_map = {
        "餐饮": "🍜", "交通": "🚕", "购物": "🛍️", "娱乐": "🎮",
        "居家": "🏠", "医疗": "💊", "教育": "📚", "人情": "🧧",
        "收入": "💰", "其他": "📝"
    }
    emoji = emoji_map.get(category, "📝")
    type_text = "收入" if bill_type == "income" else "支出"

    return f"{emoji} 记录成功！\n📌 {description}\n💵 金额：¥{amount:.2f}\n🏷️ 分类：{category}\n📅 {now.strftime('%m/%d %H:%M')}\n\n发送「本月账单」查看统计"
