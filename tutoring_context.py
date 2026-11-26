"""Select useful context for long tutoring sessions without altering stored transcripts.

Keep initial problem conditions and the latest turns. Earlier relevant
math work is excerpted verbatim, not summarized or inferred.
"""
MAX_CONTEXT_CHARS = 22000
RECENT_COUNT = 20
MARKERS = ("조건", "가정", "식", "풀이", "계산", "정답", "오답", "=", "∠", "넓이", "길이", "각", "왜")


def _line(message, content_limit=1500):
    role = "학생" if message.get("role") == "user" else "AI 수학쌤"
    content = str(message.get("content") or "")
    if len(content) > content_limit:
        content = content[:content_limit - 30] + "…[이하 일부 생략]"
    return f"[{role}]: {content}"


def build_tutoring_context(messages, max_chars=MAX_CONTEXT_CHARS):
    """Initial conditions + important intermediate turns + latest 20 turns.

    Full unabridged records are maintained separately by the storage layer.
    """
    if not messages:
        return "(없음)"
    if max_chars < 4000:
        raise ValueError("The context limit is too small")
    initial_end = min(2, len(messages))
    recent_begin = max(initial_end, len(messages) - RECENT_COUNT)
    important = []
    for i in range(initial_end, recent_begin):
        msg = messages[i]
        if any(marker in str(msg.get("content") or "") for marker in MARKERS):
            important.append(i)
    # Favor more recent intermediate evidence without inventing a summary.
    selected = list(range(initial_end)) + important[-12:] + list(range(recent_begin, len(messages)))
    segments = []
    for i in selected:
        limit = 2400 if i < initial_end else (900 if i < recent_begin else 1500)
        segments.append((i, _line(messages[i], limit)))
    result = "\n".join(line for _, line in segments)
    if len(result) <= max_chars:
        return result
    # Reduce intermediate turns before shrinking the latest conversation.
    for i in range(len(segments) - 1, -1, -1):
        index, line = segments[i]
        if initial_end <= index < recent_begin:
            segments.pop(i)
            result = "\n".join(item for _, item in segments)
            if len(result) <= max_chars:
                return result
    # Clip old recent turns while retaining the earliest conditions and latest turns.
    for i in range(len(segments)):
        index, line = segments[i]
        if initial_end <= index < len(messages) - 4 and len(line) > 220:
            segments[i] = (index, line[:215] + "…")
            result = "\n".join(item for _, item in segments)
            if len(result) <= max_chars:
                return result
    return result[:max_chars - 25] + "\n[일부 과거 대화 생략]"
