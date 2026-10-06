# Adsterra placements

## 728x90 Banner 31565233

Integrated the publisher-supplied configuration key
`c1607918ab2d91ade8037d36a7b7c333` and exact script URL
`https://bellnewyork.org/22/c1607918ab2d91ade8037d36a7b7c333`.
The embed runs inside a fixed 728x90 srcdoc iframe so provider document.write
cannot replace the app. It loads once only after the viewport is at least 800px
and the visible slot has at least 728px available, including after resizing.
The containing area is hidden on smaller screens without scaling the creative.
The frame sandbox permits scripts but blocks popups and top-level navigation.
Live provider rendering and click compatibility in this sandbox are unverified;
check with the provider before publishing if the sandbox prevents serving.
Blocked/failed ads leave the labeled reserved area and do not block app controls.

## Native Banner 31565232

Integrated the exact publisher-supplied async script
`https://bellnewyork.org/21/173bd25a12e240a688efa71a28dc9bbf`, preserving
`async="async"` and `data-cfasync="false"`, with matching container
`container-173bd25a12e240a688efa71a28dc9bbf`.
The labeled, full-width native area remains visible on mobile and desktop after
results/history and before the PWA install button. A 180px minimum height reserves
space; the area can grow in normal flow as native content loads. The container
fits its wrapper. Actual third-party creative layout and ad content are unverified.

## Activation constraints

- Both slots now contain the supplied embeds. The local
  slot IDs are wrappers, not guessed provider container IDs.
- Keep the visible Advertisement label, normal document flow and separation
  from Analyze, quality selection, job progress and real Download controls.
- Keep the leaderboard at its original 728x90 size. Its entire area is hidden
  below 800px viewport width. Before loading its script, check the wrapper has
  at least 728px available; hiding CSS alone must not load mobile impressions.
  Use a provider-supported loading method and avoid delayed document.write
  against the main document. Do not scale, crop or disguise a creative to fit.
- The native slot follows downloader results/history at a content break.
  Verify the actual provider creative responds within the wrapper on mobile;
  reserved-space CSS alone cannot verify third-party creative behavior.
- Check both themes, narrow screens, ad failure/blocking and download success.
- Preserve manifest, service-worker registration and installation behavior.
  Do not cache third-party ad responses in the service worker.
- Use only these approved banner units. No Popunder, Smartlink, Social Bar,
  anti-adblock or deceptive download-like ads.
- Adult ads are currently enabled and locked for these placements in the
  Adsterra publisher account. Do not describe them as disabled or imply that
  visitors can turn them off through this site. Any future change requires
  verification of the provider/account options and supplied unit codes.
- The About, Privacy, Terms, and Contact pages are ad-free. The Privacy Policy
  discloses the current advertising setting and third-party data processing.
