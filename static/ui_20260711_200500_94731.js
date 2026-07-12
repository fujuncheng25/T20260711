(() => {
  const flashes = document.querySelectorAll('.flash');
  if (flashes.length > 0) {
    setTimeout(() => {
      flashes.forEach((item) => {
        item.style.opacity = '0';
        item.style.transition = 'opacity 0.3s ease';
        setTimeout(() => item.remove(), 320);
      });
    }, 3600);
  }

  const cameraButton = document.getElementById('camera-button');
  const pickupInput = document.getElementById('pickup-image-input');
  const cameraForm = document.getElementById('camera-form');

  if (cameraButton && pickupInput && cameraForm) {
    cameraButton.addEventListener('click', () => {
      pickupInput.click();
    });

    pickupInput.addEventListener('change', () => {
      if (pickupInput.files && pickupInput.files.length > 0) {
        cameraForm.submit();
      }
    });
  }
})();
