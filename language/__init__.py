from .translations import TRANSLATIONS

_ROOT_PACKAGE = __package__.split('.')[0]


def get_language() -> str:
    """从插件偏好设置读取当前 UI 语言。"""
    try:
        import bpy
        prefs = bpy.context.preferences.addons[_ROOT_PACKAGE].preferences
        return getattr(prefs, 'ui_language', 'ZH')
    except Exception:
        return 'ZH'


def tr(key: str) -> str:
    """将键翻译为当前语言，找不到时返回键名本身。"""
    lang = get_language()
    if lang == 'ZH':
        zh = TRANSLATIONS['ZH'].get(key)
        if zh is not None:
            return zh
    return TRANSLATIONS['EN'].get(key, key)
