"""文本规范化：标注坐标一律使用去除换行后的文本。"""

MAX_TEXT_CHARS = 500


def normalize_text(text: str) -> str:
    """去掉 CR/LF 换行符。

    只删除换行本身（``\\r`` 与 ``\\n``），不改变其他空白字符。
    Python 字符串下标按 Unicode 码点计数，与 JSON 中的字符一一对应。
    """

    return text.replace("\r", "").replace("\n", "")
