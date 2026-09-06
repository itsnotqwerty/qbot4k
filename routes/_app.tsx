import type { PageProps } from "fresh";

const DESCRIPTION =
  "The first real community intelligence platform for streamers — moderation command, explainable signals, and live ops across Discord and Twitch, with evidence before action.";

export default function App({ Component, url }: PageProps) {
  const canonical = url.origin;
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>QBot4K</title>
        <meta name="description" content={DESCRIPTION} />
        <meta property="og:type" content="website" />
        <meta property="og:site_name" content="QBot4K" />
        <meta property="og:title" content="QBot4K" />
        <meta property="og:description" content={DESCRIPTION} />
        <meta property="og:url" content={canonical} />
        <meta property="og:image" content={`${canonical}/og-card.png`} />
        <meta property="og:image:width" content="1200" />
        <meta property="og:image:height" content="630" />
        <meta
          property="og:image:alt"
          content="QBot4K — the first real community intelligence platform for streamers"
        />
        <meta name="twitter:card" content="summary_large_image" />
        <meta name="twitter:title" content="QBot4K" />
        <meta name="twitter:description" content={DESCRIPTION} />
        <meta name="twitter:image" content={`${canonical}/og-card.png`} />
        <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
        <link rel="alternate icon" href="/favicon.ico" sizes="16x16 32x32" />
        <link rel="apple-touch-icon" href="/apple-touch-icon.png" />
        <link rel="stylesheet" href="/styles.css" />
      </head>
      <body>
        <Component />
      </body>
    </html>
  );
}
