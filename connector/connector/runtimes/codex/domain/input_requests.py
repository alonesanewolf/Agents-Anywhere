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
        raise ValueError("question response requires answers mapping")
    result = deepcopy(answers)
    questions = {question["id"]: question for question in params.get("questions", [])}
    for key, value in answers.items():
        if not isinstance(value, dict):
            raise ValueError("question answer must be an object")
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
