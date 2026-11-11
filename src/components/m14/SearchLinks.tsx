import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { LinkSet, SearchLink } from "@/lib/m14/search-links";

/** Full disclaimer (search-links spec §5). Shown once per page and in each card's details. */
export const SHORT_DISCLAIMER =
  "參考票價來自 Aviasales（經 Travelpayouts 取得），已換算為新台幣，金額為約略值，可能已變動。各平台顯示的價格與可訂位情況可能不同，實際價格以訂購網站為準。";
/** Shown wherever the Aviasales affiliate link appears (spec §5, Travelpayouts condition 3). */
export const AFFILIATE_DISCLOSURE = "本服務可能因你透過 Aviasales 連結訂購而獲得佣金。";
export const GENERAL_PAGE_NOTE =
  "「一般搜尋頁」只會開啟該網站的搜尋頁，不會帶入此行程：請自行輸入並確認出發地、目的地與來回日期。各平台的價格可能與 Aviasales 參考票價不同。";

const PLATFORM_NAME: Record<SearchLink["platform"], string> = {
  skyscanner: "Skyscanner",
  tripcom: "Trip.com",
  aviasales: "Aviasales",
  google: "Google 航班",
};

/** Short visible text. The accessible name (aria-label) is the full label from the builder. */
function shortLabel(l: SearchLink): string {
  if (l.platform === "aviasales") {
    return l.kind === "itinerary"
      ? "Aviasales（票價來源・聯盟連結）"
      : "Aviasales 一般搜尋頁（票價來源・聯盟連結）";
  }
  return l.kind === "itinerary"
    ? PLATFORM_NAME[l.platform]
    : `${PLATFORM_NAME[l.platform]} 一般搜尋頁`;
}

function attrs(l: SearchLink) {
  return {
    href: l.url,
    target: "_blank",
    rel: "noopener noreferrer",
    "aria-label": `${l.label}（另開新視窗）`,
    "data-platform": l.platform,
    "data-link-kind": l.kind,
  };
}

function TextLink({ link }: { link: SearchLink }) {
  return (
    <a
      {...attrs(link)}
      className="inline-flex items-center gap-1 rounded-sm text-sm text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      {shortLabel(link)}
      <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
    </a>
  );
}

interface SearchLinksProps {
  links: LinkSet;
}

/**
 * Compact search block for one reference fare. Itinerary links (route + both dates) are buttons;
 * general search pages are plain links that say 「一般搜尋頁」 in their visible text. The labelled
 * Aviasales affiliate link and the affiliate disclosure are always visible; the long notes are in
 * an expandable section (and the full disclaimer is also shown once on the page).
 */
export function SearchLinks({ links }: SearchLinksProps) {
  const itinerary = links.links.filter(
    (l) => l.kind === "itinerary" && (l.platform === "skyscanner" || l.platform === "tripcom"),
  );
  const others = links.links.filter((l) => !itinerary.includes(l));
  const anyGeneral = links.links.some((l) => l.kind === "fallback");

  return (
    <section
      aria-label="比價搜尋（另開新視窗）"
      className="mt-3 space-y-2 rounded-xl border border-border bg-background/40 p-3"
    >
      {itinerary.length > 0 && (
        <>
          <p className="text-xs text-muted-foreground">在其他網站搜尋此行程（帶入航線與日期）</p>
          <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
            {itinerary.map((l) => (
              <Button
                key={l.platform}
                asChild
                size="sm"
                variant={l.role === "primary" ? "default" : "outline"}
                className="w-full sm:w-auto"
              >
                <a {...attrs(l)}>
                  {shortLabel(l)}
                  <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                </a>
              </Button>
            ))}
          </div>
        </>
      )}
      <ul className="flex flex-wrap gap-x-4 gap-y-1">
        {others.map((l) => (
          <li key={l.platform}>
            <TextLink link={l} />
          </li>
        ))}
      </ul>
      {anyGeneral && (
        <p className="text-xs text-muted-foreground">
          「一般搜尋頁」不會帶入此行程，需自行輸入並確認航線與日期。
        </p>
      )}
      <p className="text-xs text-muted-foreground">{AFFILIATE_DISCLOSURE}</p>
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer rounded-sm font-medium text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
          連結與價格說明
        </summary>
        <div className="mt-2 space-y-1">
          {anyGeneral && <p>{GENERAL_PAGE_NOTE}</p>}
          <p>Aviasales 網站為英文介面，Skyscanner、Trip.com 與 Google 航班為繁體中文介面。</p>
          <p>{SHORT_DISCLAIMER}</p>
        </div>
      </details>
    </section>
  );
}
