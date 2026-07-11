(() => {
  const flashCards = Array.from(document.querySelectorAll('.flash-card'));
  if (flashCards.length > 0) {
    setTimeout(() => {
      flashCards.forEach((card, index) => {
        setTimeout(() => {
          card.style.transition = 'opacity .35s ease, transform .35s ease';
          card.style.opacity = '0';
          card.style.transform = 'translateY(-6px)';
          setTimeout(() => card.remove(), 400);
        }, index * 120);
      });
    }, 3800);
  }

  const groupCards = Array.from(document.querySelectorAll('.group-card'));
  groupCards.forEach((card, index) => {
    card.style.setProperty('--delay', `${index * 70}ms`);
  });
})();
