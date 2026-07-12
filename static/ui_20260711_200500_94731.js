(() => {
  const flashes = document.querySelectorAll('.flash');
  if (flashes.length === 0) {
    return;
  }

  setTimeout(() => {
    flashes.forEach((item) => {
      item.style.opacity = '0';
      item.style.transition = 'opacity 0.3s ease';
      setTimeout(() => item.remove(), 320);
    });
  }, 3600);
})();
