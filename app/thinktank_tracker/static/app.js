document.addEventListener("DOMContentLoaded", () => {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
});
document.body.addEventListener("htmx:afterSwap", () => {
  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });
});
