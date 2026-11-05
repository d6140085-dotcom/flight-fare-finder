import { useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";
import { ChevronDown, Search } from "lucide-react";
import { airport } from "@/lib/m14/catalog";
import { destinationLabel, searchDestinations } from "@/lib/m14/destination-search";
import { cn } from "@/lib/utils";

interface DestinationPickerProps {
  /** Id of the text input, so a <Label htmlFor> names it. */
  id: string;
  /** Selected destination code, or "" for none. */
  value: string;
  onChange: (code: string) => void;
  invalid?: boolean;
}

/**
 * Searchable destination selector (WAI-ARIA combobox with a grouped listbox).
 * Type a Chinese or English name or an IATA code; ↑/↓ move, Enter selects, Esc closes,
 * Home/End jump to the first/last match. Options are grouped by region.
 */
export function DestinationPicker({ id, value, onChange, invalid }: DestinationPickerProps) {
  const uid = useId();
  const listId = `${uid}-list`;
  const [query, setQuery] = useState<string | null>(null); // null = show the selected value
  const [open, setOpen] = useState(false);
  const [activeCode, setActiveCode] = useState<string | null>(null);

  const selected = value ? airport(value) : undefined;
  const { groups, best } = useMemo(() => searchDestinations(query ?? ""), [query]);
  const flat = useMemo(() => groups.flatMap((g) => g.options), [groups]);
  const count = flat.length;
  const active = flat.find((a) => a.code === activeCode) ?? null;
  const optionId = (code: string) => `${uid}-opt-${code}`;

  useEffect(() => {
    if (!open || !activeCode) return;
    const el = document.getElementById(optionId(activeCode));
    el?.scrollIntoView?.({ block: "nearest" });
    // optionId only depends on uid, which is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeCode]);

  function openList(initial?: string | null) {
    setOpen(true);
    setActiveCode(initial ?? (selected && query === null ? selected.code : (best?.code ?? null)));
  }

  function close() {
    if (query !== null && !query.trim() && value) onChange(""); // the user cleared the box
    setOpen(false);
    setQuery(null);
    setActiveCode(null);
  }

  function choose(code: string) {
    onChange(code);
    close();
  }

  function move(delta: number) {
    if (!count) return;
    const i = active ? flat.indexOf(active) : -1;
    const next = i < 0 ? (delta > 0 ? 0 : count - 1) : (i + delta + count) % count;
    setActiveCode(flat[next]!.code);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        if (!open) openList();
        else move(1);
        break;
      case "ArrowUp":
        e.preventDefault();
        if (!open) openList();
        else move(-1);
        break;
      case "Home":
      case "End":
        if (open && count) {
          e.preventDefault();
          setActiveCode(flat[e.key === "Home" ? 0 : count - 1]!.code);
        }
        break;
      case "Enter":
        // Only intercept Enter while choosing; otherwise it submits the form as usual.
        if (open) {
          e.preventDefault();
          if (active) choose(active.code);
          else if (query !== null && !query.trim()) close(); // confirms clearing the box
        }
        break;
      case "Escape":
        if (open || query !== null) {
          e.preventDefault();
          close();
        }
        break;
      case "Tab":
        if (open) close();
        break;
    }
  }

  return (
    <div className="relative">
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <input
          id={id}
          type="text"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={open && active ? optionId(active.code) : undefined}
          aria-invalid={invalid || undefined}
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="none"
          spellCheck={false}
          enterKeyHint="search"
          placeholder="搜尋城市、英文或機場代碼，例如：倫敦、Paris、SYD"
          className={cn(
            "flex h-9 w-full scroll-mt-24 rounded-md border border-input bg-background py-1 pl-9 pr-9 text-base shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring md:text-sm",
            invalid && "border-destructive",
          )}
          value={query ?? (selected ? destinationLabel(selected) : "")}
          onChange={(e) => {
            const q = e.target.value;
            setQuery(q);
            setOpen(true);
            // Emptying the box means "no destination": nothing is highlighted until the user types.
            setActiveCode(q.trim() ? (searchDestinations(q).best?.code ?? null) : null);
          }}
          onFocus={(e) => {
            e.target.select();
            // On phones, lift the box towards the top so the list isn't hidden by the keyboard.
            if (window.matchMedia?.("(max-width: 639px)").matches) {
              e.target.scrollIntoView?.({ block: "start", behavior: "smooth" });
            }
          }}
          onClick={() => {
            if (!open) openList();
          }}
          onBlur={close}
          onKeyDown={onKeyDown}
        />
        <ChevronDown className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      </div>

      <p className="sr-only" aria-live="polite">
        {open ? (count ? `找到 ${count} 個目的地` : "找不到符合的目的地") : ""}
      </p>

      {open && (
        <div
          id={listId}
          role="listbox"
          aria-label="目的地"
          className="absolute left-0 z-50 mt-1 max-h-[min(22rem,60vh)] w-full min-w-full overflow-y-auto overscroll-contain rounded-md border border-border bg-popover p-1 text-popover-foreground shadow-md sm:w-[22rem]"
          // Keep focus in the input while clicking or scrolling the list.
          onMouseDown={(e) => e.preventDefault()}
        >
          {count === 0 && (
            <p className="px-3 py-6 text-center text-sm text-muted-foreground">
              找不到「{query}」。可輸入中文城市名、英文名稱或三碼代碼。
            </p>
          )}
          {groups.map(({ region, options }) => (
            <div key={region.id} role="group" aria-labelledby={`${uid}-grp-${region.id}`}>
              <div
                id={`${uid}-grp-${region.id}`}
                role="presentation"
                className="sticky top-0 z-10 bg-popover px-2 pb-1 pt-2 text-xs font-semibold text-muted-foreground"
              >
                {region.name_zh} {region.name_en}
              </div>
              {options.map((a) => {
                const isActive = active?.code === a.code;
                const isSelected = a.code === value;
                return (
                  <div
                    key={a.code}
                    id={optionId(a.code)}
                    role="option"
                    aria-selected={isSelected}
                    aria-label={destinationLabel(a)}
                    className={cn(
                      "flex min-h-11 cursor-pointer items-center justify-between gap-3 rounded-sm px-2 py-1.5 text-sm",
                      isActive && "bg-accent text-accent-foreground",
                    )}
                    onMouseMove={() => {
                      if (!isActive) setActiveCode(a.code);
                    }}
                    onClick={() => choose(a.code)}
                  >
                    <span className="min-w-0">
                      <span className={cn("font-medium", isSelected && "text-primary")}>
                        {a.name_zh}
                      </span>{" "}
                      <span className="text-muted-foreground">{a.name_en}</span>
                      {a.type === "city" && a.airports && (
                        <span className="block text-xs text-muted-foreground">
                          城市代碼，含 {a.airports.join("、")}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 rounded border border-border px-1.5 py-0.5 font-mono text-xs tracking-wider">
                      {a.code}
                    </span>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
