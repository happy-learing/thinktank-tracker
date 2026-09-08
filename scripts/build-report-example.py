from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "app"))

from thinktank_tracker.exporters import ReportArticle, build_weekly_report_document


def article(
    author: str,
    title_en: str,
    title_zh: str,
    day: int,
    slug: str,
) -> ReportArticle:
    return ReportArticle(
        authors=tuple(value.strip() for value in author.split(";")),
        title_en=title_en,
        title_zh=title_zh,
        published_date=date(2026, 8, day),
        url=f"https://example.org/publications/{slug}/",
    )


def build(output: Path) -> None:
    groups = [
        (
            "示例智库甲（Example Institute A）",
            [
                article(
                    "Alice Example",
                    "A Single-Article Group in the Weekly Review",
                    "周览中的单篇文章分组示例",
                    7,
                    "single-article-group",
                )
            ],
        ),
        (
            "示例智库乙（Example Institute B）",
            [
                article(
                    "Bob Example;Carol Example",
                    "Regional Security Cooperation and Strategic Resilience",
                    "地区安全合作与战略韧性",
                    10,
                    "regional-security-cooperation",
                ),
                article(
                    "David Example",
                    "Technology Competition, Critical Infrastructure, and the Changing Architecture of International Economic Security",
                    "技术竞争、关键基础设施与国际经济安全架构的变化",
                    12,
                    "technology-competition-critical-infrastructure-and-economic-security",
                ),
            ],
        ),
        (
            "示例智库丙（Example Institute C）",
            [
                article(
                    "Elena Example;Farid Example",
                    f"Long-Form Sample {index}: Evidence, Institutions, and Strategic Policy Choices Across Europe and Its Global Partnerships",
                    f"长标题与跨页排版示例{index}：欧洲及其全球伙伴关系中的证据、制度与战略政策选择",
                    13 + index,
                    f"long-form-cross-page-sample-{index}-with-a-deliberately-long-url-for-layout-verification",
                )
                for index in range(1, 9)
            ],
        ),
    ]
    document = build_weekly_report_document(
        date(2026, 8, 7),
        date(2026, 8, 21),
        groups,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    document.core_properties.title = "智库周览格式示例"
    document.core_properties.subject = "单篇、多篇、长标题、跨页及超链接格式验证"
    document.core_properties.comments = "示例内容仅用于展示正式报告格式。"
    document.save(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.output.resolve())


if __name__ == "__main__":
    main()
