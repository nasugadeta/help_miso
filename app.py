"""助成金ダッシュボード - Streamlit アプリ"""
import streamlit as st
import json
from pathlib import Path
from datetime import datetime, date
from config import EXCLUDE_KEYWORDS

DATA_FILE = Path(__file__).parent / "data" / "grants.json"


# =============================================================================
# 認証
# =============================================================================

def check_password() -> bool:
    if st.session_state.get("authenticated"):
        return True

    st.markdown(
        "<h1 style='text-align: center; margin-top: 80px;'>助成金ダッシュボード</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='text-align: center; color: gray;'>manma 内部ツール</p>",
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        password = st.text_input("パスワード", type="password", key="pw_input")
        if st.button("ログイン", use_container_width=True):
            if password == st.secrets.get("password", ""):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("パスワードが正しくありません")
    return False


# =============================================================================
# データ読み込み・フィルタ
# =============================================================================

@st.cache_data(ttl=300)
def load_grants() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_updated": None, "grants": []}


def apply_exclude_filter(grants: list) -> list:
    """除外キーワードを含む助成金をリアルタイムで除外。"""
    result = []
    for g in grants:
        target = g.get("name", "") + g.get("categories", "")
        if not any(kw in target for kw in EXCLUDE_KEYWORDS):
            result.append(g)
    return result


# =============================================================================
# ヘルパー
# =============================================================================

def days_until_deadline(deadline_str: str) -> int | None:
    if not deadline_str:
        return None
    try:
        dl = datetime.strptime(deadline_str, "%Y-%m-%d").date()
        return (dl - date.today()).days
    except ValueError:
        return None


def format_amount(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}億円"
    if value >= 10_000:
        return f"{value // 10_000}万円"
    return f"{value:,}円"


def deadline_badge(deadline_str: str) -> str:
    days = days_until_deadline(deadline_str)
    if days is None:
        return "不明"
    if days < 0:
        return f"~~{deadline_str}~~ (終了)"
    if days <= 14:
        return f"**:red[{deadline_str}（残り{days}日）]**"
    if days <= 30:
        return f"**:orange[{deadline_str}（残り{days}日）]**"
    return f"{deadline_str}（残り{days}日）"


# =============================================================================
# メインUI
# =============================================================================

def main():
    st.set_page_config(
        page_title="助成金ダッシュボード",
        page_icon="📋",
        layout="wide",
    )

    if not check_password():
        return

    data = load_grants()
    raw_grants = data.get("grants", [])
    last_updated = data.get("last_updated", "不明")

    # 除外フィルタをリアルタイム適用
    grants = apply_exclude_filter(raw_grants)

    # --- ヘッダー ---
    st.title("📋 助成金ダッシュボード")
    st.caption(f"最終更新: {last_updated}")

    if not grants:
        st.info("まだ助成金データがありません。スクレイパーを実行してください。")
        return

    # --- サマリーカード ---
    new_count = sum(1 for g in grants if g.get("is_new"))
    active_count = sum(1 for g in grants if g.get("status") == "募集中")
    expiring_count = sum(
        1 for g in grants
        if days_until_deadline(g.get("deadline", "")) is not None
        and 0 <= days_until_deadline(g.get("deadline", "")) <= 30
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("総件数", f"{len(grants)}件")
    col2.metric("新着", f"{new_count}件", delta=f"+{new_count}" if new_count else None)
    col3.metric("募集中", f"{active_count}件")
    col4.metric("締切30日以内", f"{expiring_count}件",
                delta=f"{expiring_count}件" if expiring_count else None,
                delta_color="inverse")

    st.divider()

    # --- サイドバー: フィルタ ---
    st.sidebar.header("フィルター")

    sources = sorted(set(g.get("source", "不明") for g in grants))
    selected_sources = st.sidebar.multiselect("情報源", sources, default=sources)

    statuses = sorted(set(g.get("status", "不明") for g in grants))
    selected_statuses = st.sidebar.multiselect("ステータス", statuses, default=statuses)

    new_only = st.sidebar.checkbox("新着のみ")

    keyword_filter = st.sidebar.text_input("キーワード検索")

    sort_options = {
        "締切日（近い順）": lambda g: g.get("deadline") or "9999-12-31",
        "金額（高い順）": lambda g: g.get("amount_value") or 0,
        "発見日（新しい順）": lambda g: g.get("found_date", ""),
    }
    sort_key = st.sidebar.selectbox("並び替え", list(sort_options.keys()))
    reverse = sort_key != "締切日（近い順）"

    # --- フィルタ適用 ---
    filtered = []
    for g in grants:
        if g.get("source", "不明") not in selected_sources:
            continue
        if g.get("status", "不明") not in selected_statuses:
            continue
        if new_only and not g.get("is_new"):
            continue
        if keyword_filter:
            search_text = " ".join([
                g.get("name", ""), g.get("summary", ""),
                g.get("organization", ""), g.get("categories", ""),
            ])
            if keyword_filter not in search_text:
                continue
        filtered.append(g)

    filtered.sort(key=sort_options[sort_key], reverse=reverse)

    st.markdown(f"**{len(filtered)}件** 表示中（全{len(grants)}件中）")

    # --- 助成金一覧 ---
    for grant in filtered:
        is_new = grant.get("is_new", False)
        title_prefix = "🆕 " if is_new else ""
        deadline_str = grant.get("deadline", "")
        days = days_until_deadline(deadline_str)
        amount_label = grant.get("amount_text") or format_amount(grant.get("amount_value"))

        # 締切の色付き表示
        if not deadline_str:
            deadline_label = "締切不明"
        elif days is not None and days < 0:
            deadline_label = f"~~{deadline_str}~~"
        elif days is not None and days <= 14:
            deadline_label = f":red[{deadline_str}（残{days}日）]"
        elif days is not None and days <= 30:
            deadline_label = f":orange[{deadline_str}（残{days}日）]"
        else:
            deadline_label = deadline_str if deadline_str else "締切不明"

        # タイトル行に金額・締切を表示
        header = (
            f"{title_prefix}**{grant['name']}**"
            f"　　💰 {amount_label}"
            f"　　📅 {deadline_label}"
            f"　　`{grant.get('source', '')}`"
        )

        with st.expander(header, expanded=is_new):
            c1, c2 = st.columns([3, 1])

            with c1:
                if grant.get("organization"):
                    st.markdown(f"**助成団体:** {grant['organization']}")
                if grant.get("summary"):
                    st.markdown(f"**概要:** {grant['summary'][:300]}")

            with c2:
                st.markdown(f"**ステータス:** {grant.get('status', '不明')}")
                st.markdown(f"**発見日:** {grant.get('found_date', '不明')}")
                st.markdown(f"**地域:** {grant.get('region', '指定なし')}")

            st.markdown(f"[詳細ページを開く]({grant.get('url', '#')})")


if __name__ == "__main__":
    main()
