"""工作流与售前事件的展示名称；内部 kind 保持稳定。"""

EVENT_LABELS = {
    "open": "创建了待办", "claim": "领取了待办", "start": "开始处理待办", "release": "退回了待办",
    "transfer": "转交了待办", "reassign": "改派了待办", "complete": "完成了待办", "decline": "拒绝了待办",
    "cancel": "取消了待办", "cancel_subject": "取消了待办", "pause": "暂停了待办", "pause_subject": "暂停了待办",
    "resume": "恢复了待办", "resume_subject": "恢复了待办",
    "inquiry.created": "创建询价单", "inquiry.updated": "修改询价单", "inquiry.deleted": "删除询价草稿",
    "inquiry.submitted": "提交询价", "inquiry.withdrawn": "撤回询价", "inquiry.resubmitted": "重新提交询价",
    "inquiry.lines_appended": "追加询价行", "inquiry.closed": "结束询价", "inquiry.copied": "复制为新询价",
    "inquiry.part_bound": "绑定已有型号", "inquiry.part_created": "新建并绑定型号",
    "offer.stale": "报价已标记为旧版本", "offer.draft_saved": "保存报价草稿", "offer.decision_saved": "保存沿用或不报草稿",
    "offer.replied": "回复报价", "inquiry.urged": "催办", "offer.updated": "更新报价", "offer.withdrawn": "撤销报价",
    "offer.published": "发布主动报价", "offer.daily_rebuilt": "更新每日报价快照",
}


def event_label(kind):
    return EVENT_LABELS.get(kind, "业务动态")
