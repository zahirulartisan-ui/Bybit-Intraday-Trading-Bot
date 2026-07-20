ROUTER_MODES = {"conservative", "balanced", "aggressive"}


def normalize_mode(mode):
    mode = str(mode or "balanced").lower()
    return mode if mode in ROUTER_MODES else "balanced"


def vote_strength(vote_item):
    try:
        return abs(float(vote_item.get("strength") or 0))
    except (TypeError, ValueError):
        return 0.0


def route_votes(votes, mode="balanced"):
    mode = normalize_mode(mode)
    buy_votes = [item for item in votes if item["signal"] == "Buy"]
    sell_votes = [item for item in votes if item["signal"] == "Sell"]
    required = 2 if mode == "conservative" else 1
    if mode == "aggressive":
        buy_score = len(buy_votes) + sum(vote_strength(item) for item in buy_votes) / 100
        sell_score = len(sell_votes) + sum(vote_strength(item) for item in sell_votes) / 100
        if buy_votes and buy_score > sell_score:
            leader = max(buy_votes, key=vote_strength)
            return {
                "decision": "Buy",
                "confidence": len(buy_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Buy from {leader['engine']}",
            }
        if sell_votes and sell_score > buy_score:
            leader = max(sell_votes, key=vote_strength)
            return {
                "decision": "Sell",
                "confidence": len(sell_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Sell from {leader['engine']}",
            }
    if len(buy_votes) >= required and not sell_votes:
        leader = max(buy_votes, key=vote_strength)
        return {
            "decision": "Buy",
            "confidence": len(buy_votes),
            "requiredVotes": required,
            "mode": mode,
            "reason": f"{mode.title()} router approved Buy from {leader['engine']}",
        }
    if len(sell_votes) >= required and not buy_votes:
        leader = max(sell_votes, key=vote_strength)
        return {
            "decision": "Sell",
            "confidence": len(sell_votes),
            "requiredVotes": required,
            "mode": mode,
            "reason": f"{mode.title()} router approved Sell from {leader['engine']}",
        }
    if buy_votes and sell_votes:
        reason = "Router waiting because Buy/Sell engines conflict"
    elif mode == "conservative":
        reason = "Router waiting for 2 matching engine votes"
    else:
        reason = "Router waiting for at least 1 actionable engine vote"
    return {
        "decision": "WAIT",
        "confidence": max(len(buy_votes), len(sell_votes)),
        "requiredVotes": required,
        "mode": mode,
        "reason": reason,
    }
