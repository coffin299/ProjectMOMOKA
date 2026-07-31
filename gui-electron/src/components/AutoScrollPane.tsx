import { useEffect, useRef, useState, type ReactNode, type UIEvent } from "react";

type Props = {
  className?: string;
  /** 依存が変わったら自動スクロールするトリガ */
  deps: unknown;
  /** bottom=末尾追従 / top=先頭追従 */
  direction?: "bottom" | "top";
  children: ReactNode;
};

/**
 * カード内など overflow 領域の自動スクロール。
 * ユーザーが端から離れたら一時停止、端に戻ったら再開。
 */
export function AutoScrollPane({
  className,
  deps,
  direction = "bottom",
  children,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [stick, setStick] = useState(true);

  useEffect(() => {
    if (!stick) return;
    const el = ref.current;
    if (!el) return;
    // DOM 反映後にスクロール（履歴一括挿入対策で2回）
    const scroll = () => {
      if (direction === "top") {
        el.scrollTop = 0;
      } else {
        el.scrollTop = el.scrollHeight;
      }
    };
    scroll();
    const id = window.requestAnimationFrame(scroll);
    return () => window.cancelAnimationFrame(id);
  }, [deps, stick, direction]);

  const onScroll = (_ev: UIEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el) return;
    if (direction === "top") {
      setStick(el.scrollTop < 40);
    } else {
      const atBottom =
        el.scrollHeight - el.scrollTop - el.clientHeight < 48;
      setStick(atBottom);
    }
  };

  return (
    <div className={className} ref={ref} onScroll={onScroll}>
      {children}
    </div>
  );
}
