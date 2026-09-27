"""Native question schema to the shared runtime inputRequest form contract."""

from copy import deepcopy


def question_action(params):
    questions = []
    for question in params.get("questions", []):
        questions.append(
            {
                "id": question["id"],
                "prompt": question.get("question", ""),
                "header": question.get("header"),
                "multiple": question.get("multiple", False),
                "allowCustom": question.get("isOther", True),
                "options": [
                    {
                        "id": str(index),
                        "label": option["label"],
                        "description": option.get("description"),
                    }
                    for index, option in enumerate(question.get("options") or [])
                ],
            }
        )
    return {
        "actionId": "submit",
        "label": "Submit",
        "style": "primary",
        "input": {
            "required": True,
            "schema": {
                "type": "object",
                "required": ["answers"],
                "properties": {"answers": {"type": "object"}},
            },
            "uiSchema": {
                "component": "inputRequest",
                "version": 1,
                "questions": questions,
            },
        },
    }


def question_response(params, data):
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("question response requires answers mapping")  # noqa: TRY004 - missing required response
    result = deepcopy(answers)
    questions = {question["id"]: question for question in params.get("questions", [])}
    for key, value in answers.items():
        if not isinstance(value, dict):
            raise ValueError("question answer must be an object")  # noqa: TRY004 - correctable response validation
        if "optionIds" not in value and "customText" not in value:
            continue  # Exact native {answers: [...]} and unknown native fields survive.
        if key not in questions:
            raise ValueError("unknown question")
        options = questions[key].get("options") or []
        chosen = []
        for option_id in value.get("optionIds", []):
            if not isinstance(option_id, str) or option_id not in {
                str(i) for i in range(len(options))
            }:
                raise ValueError("unknown question option")
            chosen.append(options[int(option_id)]["label"])
        custom = value.get("customText")
        if custom:
            if not isinstance(custom, str):
                raise ValueError("custom answer must be text")
            chosen.append(custom)
        result[key] = {
            **{k: v for k, v in value.items() if k not in {"optionIds", "customText"}},
            "answers": chosen,
        }
    return {"answers": result}


def elicitation_actions(params):
    actions = [
        {"actionId": action, "label": action.title()}
        for action in ("accept", "decline", "cancel")
    ]
    if params.get("mode") == "form":
        import json

        form = question_action(
            {
                "questions": [
                    {
                        "id": "content",
                        "question": "Enter a JSON response matching this schema: "
                        + json.dumps(
                            params.get("requestedSchema", {}), ensure_ascii=False
                        ),
                        "options": [],
                    }
                ]
            }
        )
        actions[0]["input"] = form["input"]
    return tuple(actions)


def elicitation_response(params, action_id, data):
    import json

    from jsonschema import Draft202012Validator

    payload = {
        "action": action_id,
        **{key: data[key] for key in ("content", "_meta") if key in data},
    }
    if action_id == "accept" and params.get("mode") == "form":
        if "content" not in payload and isinstance(data.get("answers"), dict):
            text = data["answers"].get("content", {}).get("customText")
            if not isinstance(text, str):
                raise ValueError("MCP form requires a JSON response")
            payload["content"] = json.loads(text)
        errors = list(
            Draft202012Validator(params.get("requestedSchema") or {}).iter_errors(
                payload.get("content")
            )
        )
        if errors:
            raise ValueError("Invalid MCP response: " + errors[0].message)
    return payload
