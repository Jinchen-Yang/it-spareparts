"""人工新建入口的类别校验；使用主数据编辑器公开的同一份字典。"""

from app.services import taxonomy


def validate_categories(category_major: str | None, category_minor: str | None) -> None:
    if any(value is not None and not isinstance(value, str)
           for value in (category_major, category_minor)):
        raise ValueError("类别必须为主数据字典中的文本名称")
    major = (category_major or "").strip()
    minor = (category_minor or "").strip()
    if not major:
        if minor:
            raise ValueError("选择二级类别前请先选择一级类别")
        return
    major_code = next((code for code, name in taxonomy.CATEGORY_NAMES.items()
                       if len(code) == 2 and name == major), None)
    if major_code is None:
        raise ValueError("一级类别不在主数据字典中，请重新选择")
    if minor and not any(name == minor and len(code) == 4 and code.startswith(major_code)
                         for code, name in taxonomy.CATEGORY_NAMES.items()):
        raise ValueError("二级类别不属于所选一级类别，请重新选择")
