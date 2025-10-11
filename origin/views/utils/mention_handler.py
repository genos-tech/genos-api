class extractMentionedUsers:
    def __init__(self):
        self.mentioned_user_ids = set()

    @staticmethod
    def _check_user(content):
        _mentioned_user_ids = set()
        for c in content:
            if "type" in c and c["type"] == "mention":
                _mentioned_user_ids.add(c["props"]["userId"])
        return _mentioned_user_ids

    def extract(self, message):
        for m in message:
            if isinstance(m, dict):
                if "content" in m:
                    self.mentioned_user_ids.update(self._check_user(m["content"]))
                if "children" in m and m["children"]:
                    self.extract(m["children"])
