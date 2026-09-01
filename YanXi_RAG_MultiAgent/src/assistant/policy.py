"""Final routing/authority policy, separate from model opinions and caller claims."""
import re

HUMAN_TYPES = {"family", "friend", "leader", "colleague", "client", "general"}

def route(scene, risk, owner, profile, call_text="", needs_owner=False):
    level = int(risk["risk_level"][0])
    mode = owner.get("presence_mode", "free")
    requested = scene["requested_action"]
    emergency = scene["urgency"] == "high"
    notes = []
    if profile.get("list_status") == "blacklist":
        return {"action": "reject", "reason": "机主手动黑名单", "needs_owner": False, "emergency": emergency}
    if level >= 4:
        return {"action": "reject", "reason": "当前来电存在高风险，号码熟悉程度不能覆盖风险", "needs_owner": False, "emergency": emergency}
    if level == 3 or risk["handle_suggestion"] in ("拒绝", "拦截"):
        return {"action": "reject", "reason": "风险Agent建议拒绝此类来电", "needs_owner": False, "emergency": emergency}
    limited_meal_change = (not needs_owner and owner.get("allow_order_changes") is True and requested == "change_order"
        and scene["type_id"] == "food_delivery" and level == 1
        and not re.search(r"补差价|补钱|加钱|额外|转账|付款|支付|退款|取消|验证码", call_text))
    needs_owner = (needs_owner or scene["needs_owner_decision"] or level == 2
        or risk["handle_suggestion"] == "转人工"
        or requested in {"payment", "refund", "schedule", "identity", "change_order"}
        or (requested == "dropoff" and not owner.get("allowed_dropoff")))
    if limited_meal_change:
        needs_owner = False
        notes.append("仅允许无额外费用、同价同类的餐品替换")
    trusted_contact = (profile.get("list_status") == "whitelist" or profile.get("trust_score", 50) >= 80)
    wants_owner = needs_owner or emergency or (trusted_contact and scene["type_id"] in HUMAN_TYPES)
    if mode == "driving":
        action = "record"
        reason = "驾车期间不建议即时接听；紧急事项也只生成高优先级提醒"
    elif mode == "dnd":
        action = "forward" if emergency else "record"
        reason = "免打扰：仅紧急事项建议联系机主" if emergency else "免打扰：非紧急来电记录留言"
    elif mode == "busy":
        action = "forward" if emergency else "record"
        reason = "忙碌：紧急事项建议联系机主" if emergency else "忙碌：记录诉求，不替机主承诺回电时限"
    elif wants_owner:
        action, reason = "forward", "本事项需本人确认或优先查看；仅生成建议，不实际转接"
    elif scene["info_completeness"] == "不足" and requested not in {"notify"}:
        action, reason = "ask", "仅追问本次处理必需且尚未给出的信息"
    else:
        action, reason = "summary_card", "结合实际诉求代为回复并记录摘要"
    return {"action": action, "reason": reason, "needs_owner": bool(needs_owner),
            "emergency": emergency, "limited_meal_change": limited_meal_change, "notes": notes}

def reject_reply(risk, blacklisted=False):
    if blacklisted:
        return "您好，机主已设置不接受这个号码的来电。"
    if int(risk["risk_level"][0]) >= 4:
        return "这项要求涉及安全风险，我不能提供敏感信息或代为付款，请通过官方渠道由机主核实。"
    return "您好，机主不需要相关推广服务，谢谢。"

def guard_reply(reply, scene, policy, owner, call_text):
    """Catch known unsupported claims; not advertised as complete semantic verification."""
    issues = []
    requested = scene["requested_action"]
    # Known authority-sensitive intents use deterministic wording. We do not rely
    # on a blacklist of a few verbs to decide whether money/location changes occur.
    if requested in {"payment", "refund", "schedule", "identity", "change_order"} and policy["needs_owner"]:
        note = {"payment": "付款或借款", "refund": "退款、取消或相关方案", "schedule": "预约或时间安排",
                "identity": "身份核验及相关信息", "change_order": "餐品或订单变更"}[requested]
        return (f"您好，我是AI代接助手。您提到的情况已记录：{scene['summary'][:180]}。"
                f"{note}需要机主本人确认，我不能代为同意或提供敏感信息。"), ["权限敏感事项采用明确的待本人确认回复"]
    if requested == "dropoff":
        location = owner.get("allowed_dropoff", "").strip()
        if not location:
            return (f"您好，我是AI代接助手。您说明的投递情况已记录：{scene['summary'][:180]}。"
                    "新的投递地点需要机主确认，我不能代为指定或更改。"), ["未授权投递地点，不采纳模型擅自指定的位置"]
        return f"您好，机主授权的投递位置是{location}。如果该处无法使用，请告知情况，其他位置需机主确认，谢谢。", ["投递指引限定在机主明确授权的位置"]
    if policy.get("limited_meal_change"):
        return "您好，机主授权在无额外费用、同价同类的前提下替换餐品；如不满足这些条件，请先保留订单，等机主本人确认，谢谢。", ["餐品变更严格限定在机主明确授权范围内"]
    if re.search(r"(?:已经|已)(?:为您|帮您|给您|替您)?(?:报警|转接成功|接通|拨打|发送短信|发了短信)", reply):
        issues.append("回复声称执行了程序没有的外部操作")
    if re.search(r"正在(?:为您|帮您|给您|替您)?(?:转接|接通|拨打|报警|发送短信)", reply):
        issues.append("回复声称正在执行程序没有的外部操作")
    if "上课" in reply and "上课" not in owner.get("reason", ""):
        issues.append("回复沿用了未设置的上课状态")
    if "北门" in reply and "北门" not in call_text + owner.get("allowed_dropoff", ""):
        issues.append("回复编造了历史示例中的地点")
    if re.search(r"(?:下课后|第一时间|一定|保证|马上).{0,8}(?:回电|给您回|联系您|回来|到达)", reply) and not owner.get("callback_note"):
        issues.append("回复替机主承诺了未授权时限")
    if re.search(r"我是(?:机主|本人)|(?:妈|爸|奶奶)[，,].{0,5}我(?:在|正|会|马上)", reply):
        issues.append("回复冒充机主本人")
    if policy["action"] != "forward" and re.search(r"正在转接|现在接通|马上接听|请(?:他|机主)立即接听", reply):
        issues.append("回复与当前状态的留言策略矛盾")
    affirmative = re.search(r"(?:同意|好的|可以|没问题|帮(?:他|机主))[^。！？]{0,18}(?:补差价|补钱|付款|转账|支付|退款|取消订单|改约|换套餐)", reply)
    if affirmative and not re.search(r"不能|不可以|不同意|无法|需要.{0,8}确认", affirmative.group()):
        if not (policy.get("limited_meal_change") and "换套餐" in affirmative.group()):
            issues.append("回复可能代替机主同意费用或订单变更")
    if not issues:
        return reply, []
    subject = scene["summary"][:180]
    if policy["needs_owner"]:
        replacement = f"您好，我是AI代接助手。您提到的情况已记录：{subject}。这件事需要机主本人确认，我不能代为同意变更或付费。"
    else:
        prefix = {"free": "", "busy": "机主目前不方便接听。", "dnd": "机主目前开启免打扰。", "driving": "机主正在驾车。"}.get(owner.get("presence_mode"), "")
        replacement = f"您好，我是AI代接助手。{prefix}已记录您告知的情况：{subject}。谢谢。"
    return replacement, issues
