from datetime import datetime


def needs_google_search(message: str) -> bool:
    msg = message.lower()

    search_triggers = [
        "latest",
        "recent",
        "currently",
        "current",
        "today",
        "yesterday",
        "this week",
        "this month",
        "this year",
        "right now",
        "nowadays",
        "new update",
        "latest update",
        "news",
        "breaking",
        "trending",
        "what happened",
        "controversy",
        "controversial",
        "released",
        "release date",
        "available now",
        "price today",
        "current price",
        "stock price",
        "score",
        "who won",
        "winner of",
        "latest version",
        "new episode",
        "new season",
    ]

    for trigger in search_triggers:
        if trigger in msg:
            return True

    current_year = datetime.now().year

    recent_years = [
        str(current_year),
        str(current_year - 1),
        str(current_year - 2),
    ]

    for year in recent_years:
        if year in msg:
            return True

    return False