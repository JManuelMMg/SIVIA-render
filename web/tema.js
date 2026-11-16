/* Tema claro u oscuro de SIVIA. La elección se guarda en el navegador (sivia_tema) y la comparten todas las
   pantallas, incluido el tablero. Sin elección guardada, se sigue el tema del sistema operativo. */
(() => {
  const raiz = document.documentElement;
  const sistema = () => (matchMedia("(prefers-color-scheme: light)").matches ? "claro" : "oscuro");
  const efectivo = () => raiz.dataset.tema || sistema();
  window.siviaTema = efectivo;
  document.addEventListener("click", ev => {
    const boton = ev.target.closest("[data-cambia-tema]");
    if (!boton) return;
    const nuevo = efectivo() === "claro" ? "oscuro" : "claro";
    raiz.dataset.tema = nuevo;
    try { localStorage.setItem("sivia_tema", nuevo); } catch (_) {}
    document.dispatchEvent(new CustomEvent("sivia-tema", { detail: nuevo }));
  });
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (!raiz.dataset.tema) document.dispatchEvent(new CustomEvent("sivia-tema", { detail: efectivo() }));
  });
})();
