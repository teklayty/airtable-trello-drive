from __future__ import annotations

from io import BytesIO

import matplotlib.pyplot as plt


def _save(fig) -> bytes:
    buf = BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=180, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def bar_chart(data: dict, title: str, horizontal: bool = True) -> bytes:
    items = sorted(data.items(), key=lambda item: item[1], reverse=True)
    if not items:
        items = [("No data", 0)]
    labels = [x[0] for x in items]
    values = [x[1] for x in items]
    fig, ax = plt.subplots(figsize=(8.2, 4.7))
    if horizontal:
        labels = labels[::-1]
        values = values[::-1]
        ax.barh(labels, values)
        ax.set_xlabel("People / events")
    else:
        ax.bar(labels, values)
        ax.set_ylabel("People / events")
        ax.tick_params(axis="x", rotation=35)
    ax.set_title(title)
    ax.grid(axis="x" if horizontal else "y", alpha=0.2)
    return _save(fig)


def donut_chart(data: dict, title: str) -> bytes:
    items = [(k, v) for k, v in data.items() if v]
    if not items:
        items = [("No data", 1)]
    labels = [x[0] for x in items]
    values = [x[1] for x in items]
    fig, ax = plt.subplots(figsize=(6.2, 5.0))
    ax.pie(values, labels=labels, autopct="%1.0f%%", startangle=90, wedgeprops={"width": 0.42})
    ax.set_title(title)
    return _save(fig)


def line_chart(labels: list[str], values: list[float], title: str, ylabel: str) -> bytes:
    fig, ax = plt.subplots(figsize=(8.2, 4.7))
    ax.plot(labels, values, marker="o")
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(alpha=0.2)
    ax.tick_params(axis="x", rotation=35)
    return _save(fig)


def funnel_chart(data: dict, title: str) -> bytes:
    items = list(data.items())
    if not items:
        items = [("No data", 0)]
    labels = [x[0] for x in items]
    values = [x[1] for x in items]
    max_value = max(values) if values else 1
    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    widths = [max_value and (v / max_value) or 0 for v in values]
    y = list(range(len(items)))
    for i, (label, value) in enumerate(items):
        ax.barh(i, widths[i], height=0.7)
        ax.text(widths[i] + 0.01, i, f"{value}", va="center")
        ax.text(0.01, i, label, va="center")
    ax.set_xlim(0, 1.15)
    ax.set_yticks([])
    ax.set_xticks([])
    ax.set_title(title)
    return _save(fig)
