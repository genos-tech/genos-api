def get(first_line):
    first_line_contents = []
    try:
        for c in first_line["content"]:
            if c["type"] == "text":
                text = str(c["text"]).strip()
                if len(text) > 0:
                    first_line_contents.append(text)
            elif c["type"] == "mention":
                first_line_contents.append("@" + c["props"]["userName"])
            elif c["type"] == "link":
                first_line_contents.append(c["content"][0]["text"])
            else:
                print("[WARN] Unexpected content type:", c["type"])
                print("[WARN] first_line:\n", first_line)
        return " ".join(first_line_contents)
    except Exception as e:
        print(e)
        print("[ERROR] first_line:", first_line)
        return "Failed to generate the first line..."
