"""地震通知用の地図描画機能。"""

from __future__ import annotations

import io
import logging
from typing import Optional

from MOMOKA.notifications.earthquake_constants import (
    map_marker_color_and_size,
    scale_to_japanese,
)

logger = logging.getLogger("EarthquakeTsunamiCog")

# 依存ライブラリが無い環境でも通知機能自体を起動できるようにする
MATPLOTLIB_AVAILABLE = False
CARTOPY_AVAILABLE = False
plt = None
ccrs = None
cfeature = None

try:
    import matplotlib

    # ヘッドレス実行で描画できるバックエンドを選ぶ
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    MATPLOTLIB_AVAILABLE = True
    logger.info("✅ Matplotlibが正常にインポートされました。")

    try:
        import japanize_matplotlib  # noqa: F401

        logger.info("✅ japanize_matplotlibが正常にインポートされました。")
    except ImportError:
        logger.info("ℹ️ japanize_matplotlibなし。代替フォントを設定します。")
        try:
            import matplotlib.font_manager as fm

            japanese_fonts = [
                "MS Gothic",
                "Yu Gothic",
                "Meiryo",
                "MS UI Gothic",
                "DejaVu Sans",
            ]
            available_fonts = [font.name for font in fm.fontManager.ttflist]
            for font in japanese_fonts:
                if font in available_fonts:
                    plt.rcParams["font.family"] = font
                    logger.info("✅ 日本語フォント設定: %s", font)
                    break
            else:
                plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
                logger.warning("⚠️ 日本語フォントが見つかりません。")
        except Exception as error:  # noqa: BLE001
            logger.debug("フォント設定エラー（続行）: %s", error)

    try:
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature

        CARTOPY_AVAILABLE = True
        logger.info("✅ Cartopyが正常にインポートされました。地図機能が有効です。")
    except ImportError as error:
        logger.warning("⚠️ Cartopyが見つかりません。地図機能は無効になります。")
        logger.error("詳細エラー: %s", error, exc_info=True)
except ImportError as error:
    logger.error("❌ Matplotlibのインポートに失敗しました: %s", error)
except Exception as error:  # noqa: BLE001
    logger.error("❌ 予期しないエラーが発生しました: %s", error, exc_info=True)


def calculate_smart_map_extent(
    lat: float,
    lon: float,
    max_scale: int,
) -> tuple[float, float, float, float]:
    """震源地の位置と震度に基づいて最適な地図表示範囲を計算する。"""
    # フィリピンを含む日本周辺の境界を定義する
    region_lon_min, region_lon_max = 118, 150
    region_lat_min, region_lat_max = 10, 46
    # 遠方震源では十分な範囲を確保する
    is_far_south = lat < 24
    is_far_west = lon < 122
    # 強い地震ほど詳細に表示する
    if max_scale >= 50:
        base_zoom = 5.0
    elif max_scale >= 40:
        base_zoom = 4.0
    else:
        base_zoom = 3.0
    # 日本域外の震源では縮尺を広げる
    if is_far_south or is_far_west:
        base_zoom = max(base_zoom, 8.0)
    lon_span = base_zoom * 2
    lat_span = base_zoom * 1.6
    # 境界からの距離を求める
    dist_to_west = lon - region_lon_min
    dist_to_east = region_lon_max - lon
    dist_to_south = lat - region_lat_min
    dist_to_north = region_lat_max - lat
    # 震源が端に寄った際に中心を補正する
    edge_threshold = base_zoom
    center_lon, center_lat = lon, lat
    if dist_to_west < edge_threshold:
        center_lon = lon + (edge_threshold - dist_to_west) * 0.5
    elif dist_to_east < edge_threshold:
        center_lon = lon - (edge_threshold - dist_to_east) * 0.5
    if dist_to_south < edge_threshold:
        center_lat = lat + (edge_threshold - dist_to_south) * 0.5
    elif dist_to_north < edge_threshold:
        center_lat = lat - (edge_threshold - dist_to_north) * 0.5
    # 中心と縮尺から表示範囲を計算する
    lon_min, lon_max = center_lon - lon_span / 2, center_lon + lon_span / 2
    lat_min, lat_max = center_lat - lat_span / 2, center_lat + lat_span / 2
    # 地図境界からはみ出た範囲を反対側へ寄せる
    if lon_min < region_lon_min:
        shift = region_lon_min - lon_min
        lon_min, lon_max = region_lon_min, min(lon_max + shift, region_lon_max)
    if lon_max > region_lon_max:
        shift = lon_max - region_lon_max
        lon_max, lon_min = region_lon_max, max(lon_min - shift, region_lon_min)
    if lat_min < region_lat_min:
        shift = region_lat_min - lat_min
        lat_min, lat_max = region_lat_min, min(lat_max + shift, region_lat_max)
    if lat_max > region_lat_max:
        shift = lat_max - region_lat_max
        lat_max, lat_min = region_lat_max, max(lat_min - shift, region_lat_min)
    return lon_min, lon_max, lat_min, lat_max


def _add_base_features(ax) -> None:
    """共通の背景、海岸線、行政境界を描画する。"""
    # 海と陸を既存デザインと同じ配色で描画する
    ax.add_feature(cfeature.OCEAN, facecolor="#2c3e50", zorder=0)
    ax.add_feature(cfeature.LAND, facecolor="#95a5a6", edgecolor="none", zorder=1)
    ax.add_feature(cfeature.COASTLINE, edgecolor="white", linewidth=1.5, zorder=3)
    try:
        # 詳細な行政境界を利用できる場合だけ追加する
        states = cfeature.NaturalEarthFeature(
            category="cultural",
            name="admin_1_states_provinces_lines",
            scale="10m",
            facecolor="none",
        )
        ax.add_feature(states, edgecolor="white", linewidth=0.6, alpha=0.5, zorder=2)
    except Exception:  # noqa: BLE001
        logger.debug("都道府県境界の追加をスキップ")
    # 背景になじむグリッド線を描く
    ax.gridlines(
        crs=ccrs.PlateCarree(),
        draw_labels=False,
        linewidth=0.5,
        color="white",
        alpha=0.3,
        linestyle="--",
    )


def generate_single_map_sync(quake: dict, info_type: str) -> io.BytesIO:
    """単一の地震の位置を地図に表示する。"""
    # 依存ライブラリがない場合は明確に失敗させる
    if not CARTOPY_AVAILABLE or plt is None:
        raise RuntimeError("地図機能は現在利用できません。")
    # 必須の震源情報を取り出す
    lat, lon = quake["lat"], quake["lon"]
    max_scale = quake["max_scale"]
    # 既存と同じ台風風デザインの描画面を作る
    fig = plt.figure(figsize=(16, 16), dpi=150, facecolor="#2c3e50")
    ax = fig.add_axes(
        [0, 0, 1, 1],
        projection=ccrs.PlateCarree(),
        facecolor="#2c3e50",
    )
    # 震源と震度に応じた表示範囲を設定する
    lon_min, lon_max, lat_min, lat_max = calculate_smart_map_extent(
        lat,
        lon,
        max_scale,
    )
    ax.set_extent([lon_min, lon_max, lat_min, lat_max], crs=ccrs.PlateCarree())
    _add_base_features(ax)
    # 情報種別に応じたタイトルを表示する
    title_prefix = "緊急地震速報" if info_type == "eew" else "地震情報"
    ax.text(
        0.5,
        0.98,
        f'{title_prefix} - 震源位置\n{quake["name"]}',
        transform=ax.transAxes,
        fontsize=18,
        fontweight="normal",
        ha="center",
        va="top",
        color="white",
        bbox=dict(
            boxstyle="round,pad=0.8",
            facecolor="black",
            edgecolor="white",
            alpha=0.8,
            linewidth=2,
        ),
    )
    # 表示範囲に入る主要都市だけを描画する
    cities = {
        "札幌": (141.35, 43.06),
        "仙台": (140.87, 38.27),
        "東京": (139.69, 35.69),
        "名古屋": (136.91, 35.18),
        "大阪": (135.50, 34.69),
        "福岡": (130.42, 33.59),
        "那覇": (127.68, 26.21),
        "マニラ": (120.98, 14.60),
    }
    for city, (city_lon, city_lat) in cities.items():
        if lon_min <= city_lon <= lon_max and lat_min <= city_lat <= lat_max:
            ax.plot(
                city_lon,
                city_lat,
                marker="^",
                color="yellow",
                markersize=8,
                zorder=8,
                transform=ccrs.Geodetic(),
                markeredgecolor="black",
                markeredgewidth=1.5,
            )
            ax.text(
                city_lon,
                city_lat + 0.15,
                city,
                fontsize=9,
                ha="center",
                color="white",
                bbox=dict(
                    boxstyle="round,pad=0.3",
                    facecolor="black",
                    edgecolor="yellow",
                    alpha=0.85,
                    linewidth=1,
                ),
                transform=ccrs.Geodetic(),
                zorder=9,
                fontweight="normal",
            )
    # 震度に対応する既存のマーカーサイズを選ぶ
    _, size = map_marker_color_and_size(max_scale, multi=False)
    # 震源を二重のマーカーとして描く
    ax.scatter(
        lon, lat, marker="x", c="red", s=size * 2, linewidths=6,
        zorder=11, transform=ccrs.Geodetic(),
    )
    ax.scatter(
        lon, lat, c="red", s=size, alpha=0.8, edgecolors="white",
        linewidths=3, zorder=10, transform=ccrs.Geodetic(), label="震源",
    )
    # 震源詳細を既存と同じ条件で組み立てる
    info_text = f"震度: {scale_to_japanese(max_scale)}\n"
    if quake["magnitude"] != -1:
        info_text += f'M{quake["magnitude"]:.1f}\n'
    if quake["depth"] != -1:
        info_text += f'深さ: {quake["depth"]}km'
    # 表示範囲に応じてラベル位置を算出する
    text_offset = (lon_max - lon_min) / 2 * 0.6
    text_y = lat - text_offset
    if text_y < lat_min + 0.5:
        text_y = lat + text_offset
    text_x = lon
    if lon < lon_min + 1:
        text_x = lon_min + 1.5
    elif lon > lon_max - 1:
        text_x = lon_max - 1.5
    ax.text(
        text_x, text_y, info_text, fontsize=13, ha="center", va="top",
        color="white",
        bbox=dict(
            boxstyle="round,pad=0.7", facecolor="black", edgecolor="red",
            linewidth=2.5, alpha=0.9,
        ),
        transform=ccrs.Geodetic(), zorder=12, fontweight="normal",
    )
    # 震源凡例を表示する
    ax.legend(
        loc="upper left", frameon=True, fontsize=12, fancybox=True,
        shadow=True, framealpha=0.9, bbox_to_anchor=(0.02, 0.92),
        facecolor="black", edgecolor="white", labelcolor="white",
    )
    # PNG をメモリへ保存して呼び出し元へ返す
    buffer = io.BytesIO()
    plt.savefig(
        buffer, format="png", dpi=150, bbox_inches="tight", pad_inches=0,
        facecolor="#2c3e50", edgecolor="none",
    )
    buffer.seek(0)
    plt.close(fig)
    return buffer


def generate_map_sync(
    quakes: list,
    min_scale: Optional[str],
    hours: Optional[int],
) -> io.BytesIO:
    """複数の地震マップ画像を生成する。"""
    # 依存ライブラリがない場合は明確に失敗させる
    if not CARTOPY_AVAILABLE or plt is None:
        raise RuntimeError("地図機能は現在利用できません。")
    # 既存と同じ台風風デザインの描画面を作る
    fig = plt.figure(figsize=(16, 16), dpi=150, facecolor="#2c3e50")
    ax = fig.add_axes(
        [0, 0, 1, 1],
        projection=ccrs.PlateCarree(),
        facecolor="#2c3e50",
    )
    # 複数表示は既存どおり日本周辺へ限定する
    ax.set_extent([128, 146, 30, 46], crs=ccrs.PlateCarree())
    _add_base_features(ax)
    # 指定期間と件数をタイトルへ反映する
    title = (
        f"地震発生地点マップ（過去{hours}時間、{len(quakes)}件）"
        if hours is not None
        else f"地震発生地点マップ（{len(quakes)}件）"
    )
    if min_scale:
        title += f"\n最小震度: {min_scale}"
    ax.text(
        0.5, 0.98, title, transform=ax.transAxes, fontsize=18,
        fontweight="normal", ha="center", va="top", color="white",
        bbox=dict(
            boxstyle="round,pad=0.8", facecolor="black", edgecolor="white",
            alpha=0.9, linewidth=2,
        ),
    )
    # 凡例は初出の震度だけを保持する
    legend_elements = {}
    for quake in quakes:
        color, size, label = map_marker_color_and_size(
            quake["max_scale"],
            multi=True,
        )
        ax.scatter(
            quake["lon"], quake["lat"], c=color, s=size, alpha=0.7,
            edgecolors="white", linewidths=1.5, zorder=5,
            transform=ccrs.Geodetic(),
        )
        if label not in legend_elements:
            legend_elements[label] = plt.scatter(
                [], [], c=color, s=120, edgecolors="white",
                linewidths=1.5, alpha=0.7,
            )
    # 震度の高い順に凡例を表示する
    scale_order = [
        "震度7", "震度6強", "震度6弱", "震度5強", "震度5弱", "震度4",
        "震度3", "震度2", "震度1",
    ]
    legend_items = [legend_elements[scale] for scale in scale_order if scale in legend_elements]
    legend_labels = [scale for scale in scale_order if scale in legend_elements]
    if legend_items:
        legend = ax.legend(
            legend_items, legend_labels, loc="upper right", frameon=True,
            fontsize=11, title="震度", title_fontsize=12, fancybox=True,
            shadow=True, framealpha=0.9, bbox_to_anchor=(0.98, 0.92),
            facecolor="black", edgecolor="white",
        )
        plt.setp(legend.get_texts(), color="white")
        plt.setp(legend.get_title(), color="white")
    # 複数表示用の主要都市を描画する
    cities = {
        "札幌": (141.35, 43.06), "東京": (139.69, 35.69),
        "名古屋": (136.91, 35.18), "大阪": (135.50, 34.69),
        "福岡": (130.42, 33.59),
    }
    for city, (lon, lat) in cities.items():
        ax.plot(
            lon, lat, marker="^", color="yellow", markersize=7, zorder=4,
            transform=ccrs.Geodetic(), markeredgecolor="black",
            markeredgewidth=1.2,
        )
        ax.text(
            lon, lat + 0.35, city, fontsize=9, ha="center", color="white",
            bbox=dict(
                boxstyle="round,pad=0.3", facecolor="black",
                edgecolor="yellow", alpha=0.85, linewidth=0.8,
            ),
            transform=ccrs.Geodetic(), zorder=4, fontweight="normal",
        )
    # PNG をメモリへ保存して呼び出し元へ返す
    buffer = io.BytesIO()
    plt.savefig(
        buffer, format="png", dpi=150, bbox_inches="tight", pad_inches=0,
        facecolor="#2c3e50", edgecolor="none",
    )
    buffer.seek(0)
    plt.close(fig)
    return buffer
