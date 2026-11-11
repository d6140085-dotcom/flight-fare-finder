import { ExternalLink } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { LinkSet, SearchLink } from "@/lib/m14/search-links";
import { cn } from "@/lib/utils";

export const SHORT_DISCLAIMER =
  "參考票價來自 Aviasales（經 Travelpayouts 取得），已換算為新台幣，金額為約略值，可能已變動。各平台顯示的價格與可訂位情況可能不同，實際價格以訂購網站為準。";
export const AFFILIATE_DISCLOSURE = "本服務可能因你透過 Aviasales 連結訂購而獲得佣金。";
export const GENERAL_PAGE_NOTE =
  "標示「一般搜尋頁」的連結只會開啟該網站的搜尋頁，不會帶入此行程：請自行輸入並確認出發地、目的地與來回日期。各平台的價格可能與 Aviasales 參考票價不同。";

interface SearchLinksProps {
  links: LinkSet;
  /** Mock/preview mode: the Aviasales marker is a test value, not this project's partner ID. */
  demoMarker: boolean;
}

function External({ link, primary }: { link: SearchLink; primary?: boolean }) {
  const general = link.kind === "fallback";
  const content = (
    <>
      <span>{link.label}</span>
      <ExternalLink className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="sr-only">（另開新視窗）</span>
    </>
  );
  const attrs = {
    href: link.url,
    target: "_blank",
    rel: "noopener noreferrer",
    "data-platform": link.platform,
    "data-link-kind": link.kind,
  };
  if (!general && (link.role === "primary" || link.role === "secondary")) {
    return (
      <Button
        asChild
        variant={primary ? "default" : "outline"}
        className="h-auto w-full justify-between whitespace-normal py-2 text-left sm:w-auto"
      >
        <a {...attrs}>{content}</a>
      </Button>
    );
  }
  return (
    <a
      {...attrs}
      className="inline-flex items-start gap-1.5 text-sm text-primary underline-offset-4 hover:underline"
    >
      {general && (
        <Badge variant="outline" className="shrink-0">
          一般搜尋頁
        </Badge>
      )}
      {content}
    </a>
  );
}

/**
 * Search links for one reference fare (spec §2.2): Skyscanner and Trip.com first; Aviasales (fare
 * source, labelled affiliate link) and Google Flights under 「更多搜尋方式」. General search pages
 * are labelled and never presented as itinerary links.
 */
export function SearchLinks({ links, demoMarker }: SearchLinksProps) {
  const main = links.links.filter((l) => l.platform === "skyscanner" || l.platform === "tripcom");
  const more = links.links.filter((l) => l.platform === "aviasales" || l.platform === "google");
  const anyGeneral = links.links.some((l) => l.kind === "fallback");
  return (
    <div className="mt-3 space-y-3" aria-label="搜尋此行程">
      {main.length > 0 && (
        <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          {main.map((l) =>
            l.kind === "itinerary" ? (
              <External key={l.platform} link={l} primary={l.role === "primary"} />
            ) : null,
          )}
        </div>
      )}
      {main.some((l) => l.kind === "fallback") && (
        <ul className="space-y-1">
          {main
            .filter((l) => l.kind === "fallback")
            .map((l) => (
              <li key={l.platform}>
                <External link={l} />
              </li>
            ))}
        </ul>
      )}
      {more.length > 0 && (
        <details className="rounded-md border border-border p-3 text-sm">
          <summary className="cursor-pointer font-medium">更多搜尋方式</summary>
          <ul className="mt-2 space-y-2">
            {more.map((l) => (
              <li key={l.platform}>
                <External link={l} />
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className={cn("space-y-1 text-xs text-muted-foreground")}>
        {anyGeneral && <p>{GENERAL_PAGE_NOTE}</p>}
        <p>{SHORT_DISCLAIMER}</p>
        <p>{AFFILIATE_DISCLOSURE}</p>
        {demoMarker && (
          <p role="note" className="font-medium text-foreground">
            開發預覽：Aviasales
            連結使用測試用標記（marker=000000），不是正式聯盟連結，請勿用於正式訂購。
          </p>
        )}
      </div>
    </div>
  );
}
