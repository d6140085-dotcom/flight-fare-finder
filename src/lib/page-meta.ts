import { useEffect } from "react";

type PageMeta = {
  title: string;
  description?: string;
  /** Extra <meta> tags keyed by `name` or `property` (e.g. "og:title", "robots"). */
  tags?: Record<string, string>;
};

function upsertMeta(key: string, content: string) {
  const attr = key.startsWith("og:") ? "property" : "name";
  let el = document.head.querySelector<HTMLMetaElement>(`meta[${attr}="${key}"]`);
  const created = !el;
  if (!el) {
    el = document.createElement("meta");
    el.setAttribute(attr, key);
    document.head.appendChild(el);
  }
  const previous = el.getAttribute("content");
  el.setAttribute("content", content);
  return () => {
    if (created) el?.remove();
    else if (previous !== null) el?.setAttribute("content", previous);
  };
}

/** Client-side replacement for TanStack Router's per-route `head()`. */
export function usePageMeta({ title, description, tags }: PageMeta) {
  const tagsKey = JSON.stringify(tags ?? {});
  useEffect(() => {
    const previousTitle = document.title;
    document.title = title;
    const restores: Array<() => void> = [];
    if (description) restores.push(upsertMeta("description", description));
    for (const [key, content] of Object.entries(JSON.parse(tagsKey) as Record<string, string>)) {
      restores.push(upsertMeta(key, content));
    }
    return () => {
      document.title = previousTitle;
      restores.reverse().forEach((restore) => restore());
    };
  }, [title, description, tagsKey]);
}
