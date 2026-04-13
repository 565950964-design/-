"""
微信公众号对接模块
将此模块与 app.py 合并，或单独运行后配置到微信公众号后台

依赖：pip install wechatpy flask
"""

import os
import re
from flask import request, make_response
from wechatpy import parse_message, create_reply
from wechatpy.utils import check_signature
from wechatpy.exceptions import InvalidSignatureException

# 从你的微信公众号后台获取的配置
WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "your_wechat_token_here")
WECHAT_ADMIN_OPENID = os.getenv("WECHAT_ADMIN_OPENID", "")

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
    from datetime import datetime

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

    # 查询指令
    if any(kw in text for kw in ["本月", "这月", "账单", "统计", "花了多少", "查询"]):
        now = datetime.now()
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
