from scripts.dashboard_routes import user_routes as ur


def test_category_inference_handles_unicode_and_never_unknown():
    cases = [
        (
            "Will São Paulo temperature exceed 30°C?",
            "highest-temperature-in-sao-paulo-on-apr-20",
            {"category": ""},
            "weather",
        ),
        (
            "Will BTC close above $95k?",
            "bitcoin-above-95000-tonight",
            {"group": ""},
            "crypto",
        ),
        (
            "Will Team A vs Team B win the championship?",
            "team-a-vs-team-b-championship",
            {"category": "sports"},
            "sports",
        ),
        (
            "¿Quién ganará la elección presidencial?",
            "presidential-election-2026",
            {"category": "politics"},
            "geopolitics",
        ),
        (
            "Totally Unseen Market ###",
            "@@@-%%%",
            {"category": "unknown"},
            "economy",
        ),
    ]

    for title, slug, raw, expected in cases:
        got = ur._infer_market_category(title, slug, raw)
        assert got == expected
        assert got not in {"unknown", "default"}
