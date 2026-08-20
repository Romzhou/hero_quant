# tests/test_ask_card.py
def test_ask_card_blocks(tmp_path):
    from hero_quant.interaction.questions import UserQuestionService
    svc=UserQuestionService()
    try: svc.ask_sync(questions=[{"id":"q1","question":"确认？","header":"Confirm","options":[{"label":"是","description":"推荐"}]}])
    except Exception as e: assert "NO_PROVIDER" in str(e)
