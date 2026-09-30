from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "threads-to-naver"
KEYRING_SERVICE = "threads-to-naver-drafts"
KEYRING_USERNAME = "threads-access-token"
KEYRING_SAVED_AT_USERNAME = "threads-access-token-saved-at"


@dataclass(frozen=True)
class Config:
    project_dir: Path
    naver_blog_id: str
    timezone: ZoneInfo
    headless: bool
    include_replies: bool
    include_reposts: bool
    max_posts_per_run: int
    naver_write_url: str
    footer_url: str
    footer_image_path: Path | None
    data_dir: Path
    profile_dir: Path
    artifacts_dir: Path
    state_db: Path
    source: str
    simplenote_tag: str
    simplenote_start_date: date | None
    simplenote_provider: str
    simplenote_store_path: Path | None
    simplenote_mcp_command: Path
    simplenote_scan_limit: int
    simplenote_max_notes_per_run: int
    simplenote_tags: tuple[str, ...] = ()
    require_generated_cover: bool = False

    @property
    def simplenote_queue_tags(self) -> tuple[str, ...]:
        """Return configured queue tags, preserving old single-tag configs."""
        return self.simplenote_tags or (self.simplenote_tag,)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        project_dir = Path(__file__).resolve().parents[2]
        path = config_path or project_dir / "config.toml"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}. Copy config.example.toml to config.toml first."
            )
        with path.open("rb") as file:
            raw = tomllib.load(file)

        data_dir = Path.home() / ".local" / "share" / APP_NAME
        footer_image_value = str(raw.get("footer_image_path", "")).strip()
        footer_image_path = (
            (path.parent / footer_image_value).resolve()
            if footer_image_value
            else None
        )
        source = str(raw.get("source", "simplenote")).strip().lower()
        if source not in {"threads", "simplenote"}:
            raise ValueError("source must be either 'threads' or 'simplenote'.")
        simplenote_store_value = str(
            raw.get(
                "simplenote_store_path",
                "~/Library/Group Containers/"
                "PZYM8XX95Q.com.automattic.SimplenoteMac/Data/"
                "Simplenote.storedata",
            )
        )
        simplenote_provider = str(
            raw.get("simplenote_provider", "local")
        ).strip().lower()
        if simplenote_provider not in {"local", "api"}:
            raise ValueError("simplenote_provider must be either 'local' or 'api'.")
        simplenote_start_value = str(raw.get("simplenote_start_date", "")).strip()
        try:
            simplenote_start_date = (
                date.fromisoformat(simplenote_start_value)
                if simplenote_start_value
                else None
            )
        except ValueError as exc:
            raise ValueError(
                "simplenote_start_date must be empty or use YYYY-MM-DD format."
            ) from exc
        simplenote_command_value = str(
            raw.get(
                "simplenote_mcp_command",
                "node_modules/.bin/simplenote-mcp",
            )
        )
        simplenote_mcp_command = Path(simplenote_command_value).expanduser()
        if not simplenote_mcp_command.is_absolute():
            simplenote_mcp_command = project_dir / simplenote_mcp_command
        raw_tags = raw.get("simplenote_tags")
        if raw_tags is None:
            legacy_tag = str(raw.get("simplenote_tag", "naver")).strip()
            simplenote_tags = (legacy_tag,) if legacy_tag else ()
        else:
            if not isinstance(raw_tags, list) or not all(
                isinstance(tag, str) for tag in raw_tags
            ):
                raise ValueError("simplenote_tags must be an array of strings.")
            simplenote_tags = tuple(
                dict.fromkeys(tag.strip() for tag in raw_tags if tag.strip())
            )
        if not simplenote_tags:
            raise ValueError("Configure at least one non-empty Simplenote queue tag.")
        return cls(
            project_dir=project_dir,
            naver_blog_id=str(raw.get("naver_blog_id", "")).strip(),
            timezone=ZoneInfo(str(raw.get("timezone", "Asia/Seoul"))),
            headless=bool(raw.get("headless", False)),
            include_replies=bool(raw.get("include_replies", False)),
            include_reposts=bool(raw.get("include_reposts", False)),
            max_posts_per_run=max(1, int(raw.get("max_posts_per_run", 20))),
            naver_write_url=str(
                raw.get(
                    "naver_write_url",
                    "https://blog.naver.com/{blog_id}/postwrite",
                )
            ),
            footer_url=str(raw.get("footer_url", "")).strip(),
            footer_image_path=footer_image_path,
            data_dir=data_dir,
            profile_dir=data_dir / "browser-profile",
            artifacts_dir=project_dir / "artifacts",
            state_db=data_dir / "state.sqlite3",
            source=source,
            simplenote_tag=simplenote_tags[0],
            simplenote_start_date=simplenote_start_date,
            simplenote_provider=simplenote_provider,
            simplenote_store_path=(
                Path(simplenote_store_value).expanduser()
                if simplenote_provider == "local"
                else None
            ),
            simplenote_mcp_command=simplenote_mcp_command,
            simplenote_scan_limit=min(
                100, max(1, int(raw.get("simplenote_scan_limit", 100)))
            ),
            simplenote_max_notes_per_run=max(
                1, int(raw.get("simplenote_max_notes_per_run", 2))
            ),
            simplenote_tags=simplenote_tags,
            require_generated_cover=bool(raw.get("require_generated_cover", False)),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
