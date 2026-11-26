import unittest
from tutoring_context import build_tutoring_context


class ContextSelectionTests(unittest.TestCase):
    def test_no_messages(self):
        self.assertEqual(build_tutoring_context([]), "(없음)")

    def test_initial_conditions_survive_40_turns(self):
        messages = [{"role": "user", "content": "처음 문제 조건: 삼각형 ABC에서 AB=10"},
                    {"role": "assistant", "content": "1단계 힌트: 높이를 찾아요"}]
        for i in range(40):
            messages.append({"role": "user" if i % 2 == 0 else "assistant",
                             "content": f"{i}번째 대화" + (" 계산 7=3+4" if i == 4 else "")})
        result = build_tutoring_context(messages)
        self.assertIn("처음 문제 조건: 삼각형 ABC에서 AB=10", result)
        self.assertIn("계산 7=3+4", result)
        self.assertIn("39번째 대화", result)

    def test_context_is_bounded_while_last_turn_survives(self):
        messages = [{"role": "user", "content": "처음 조건: x=2"},
                    {"role": "assistant", "content": "첫 힌트"}]
        messages.extend({"role": "user", "content": "계산 " + "가"*800}
                        for _ in range(60))
        messages.append({"role": "user", "content": "마지막 질문은?"})
        result = build_tutoring_context(messages)
        self.assertLessEqual(len(result), 22000)
        self.assertIn("처음 조건: x=2", result)
        self.assertIn("마지막 질문은?", result)


if __name__ == "__main__":
    unittest.main()
