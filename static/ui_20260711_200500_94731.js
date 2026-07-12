(() => {
  const escapeHtml = (text) => String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

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

  const pollAnchor = document.getElementById('notification-poll-anchor');
  const notifyButton = document.getElementById('enable-browser-notify');
  const messageBar = document.querySelector('.message-bar');
  let messageList = document.querySelector('.message-bar .msg-list');
  if (pollAnchor) {
    const pollUrl = pollAnchor.dataset.pollUrl || '';
    let latestId = Number(pollAnchor.dataset.initialId || '0');

    const refreshNotifyButton = () => {
      if (!notifyButton) {
        return;
      }
      if (!('Notification' in window)) {
        notifyButton.textContent = '浏览器不支持系统通知';
        notifyButton.disabled = true;
        return;
      }
      if (Notification.permission === 'granted') {
        notifyButton.textContent = '系统通知已开启';
        notifyButton.disabled = true;
      } else if (Notification.permission === 'denied') {
        notifyButton.textContent = '系统通知已被禁用';
        notifyButton.disabled = true;
      } else {
        notifyButton.textContent = '开启系统通知';
        notifyButton.disabled = false;
      }
    };

    if (notifyButton && 'Notification' in window) {
      notifyButton.addEventListener('click', async () => {
        try {
          const permission = await Notification.requestPermission();
          if (permission === 'granted' && navigator.vibrate) {
            navigator.vibrate([100, 70, 120]);
          }
        } catch (error) {
          console.error(error);
        }
        refreshNotifyButton();
      });
    }
    refreshNotifyButton();

    const prependMessage = (item) => {
      if (!messageList && messageBar) {
        const emptyText = messageBar.querySelector('.muted');
        if (emptyText) {
          emptyText.remove();
        }
        messageList = document.createElement('ul');
        messageList.className = 'msg-list';
        messageBar.appendChild(messageList);
      }
      if (!messageList) {
        return;
      }
      const row = document.createElement('li');
      row.innerHTML = `<strong>${escapeHtml(item.created_at || '--:--')}</strong> <span>${escapeHtml(item.title || '快递到了')}</span>`;
      messageList.prepend(row);
      while (messageList.children.length > 8) {
        messageList.removeChild(messageList.lastElementChild);
      }
    };

    const triggerBrowserNotification = (item) => {
      if (!('Notification' in window) || Notification.permission !== 'granted') {
        return;
      }
      const body = item.body || '有新的到货提醒';
      new Notification(item.title || '快递到了', { body });
      if (navigator.vibrate) {
        navigator.vibrate([160, 90, 160]);
      }
    };

    const pollNotifications = async () => {
      if (!pollUrl) {
        return;
      }
      try {
        const response = await fetch(`${pollUrl}?after_id=${encodeURIComponent(String(latestId))}`, {
          credentials: 'same-origin',
        });
        if (!response.ok) {
          return;
        }
        const payload = await response.json();
        const items = Array.isArray(payload.items) ? payload.items : [];
        items.forEach((item) => {
          prependMessage(item);
          triggerBrowserNotification(item);
        });
        if (Number.isFinite(payload.max_id)) {
          latestId = Math.max(latestId, Number(payload.max_id));
        }
      } catch (error) {
        console.error(error);
      }
    };

    setInterval(pollNotifications, 15000);
  }

  const searchInput = document.getElementById('order-search-input');
  const searchList = document.getElementById('search-result-list');
  if (searchInput && searchList) {
    const searchUrl = searchList.dataset.searchUrl || '';
    let timer = null;
    let latestQuery = searchInput.value.trim();

    const renderSearchItems = (items) => {
      if (!Array.isArray(items) || items.length === 0) {
        searchList.innerHTML = '<li class="panel muted">没有找到匹配结果。</li>';
        return;
      }

      const html = items.map((item) => {
        const score = Number(item.score || 0) * 100;
        return `
          <li class="panel">
            <p><strong>单号：${escapeHtml(item.order_no || '')}</strong></p>
            <p class="muted">LCS=${escapeHtml(item.lcs || 0)}，匹配度=${score.toFixed(2)}%</p>
            <p class="muted">拍照人：${escapeHtml(item.uploader_name || '')}，时间：${escapeHtml(item.created_at || '')}</p>
            <img class="photo" src="${escapeHtml(item.image_url || '')}" alt="上传图片" loading="lazy">
          </li>
        `;
      }).join('');
      searchList.innerHTML = html;
    };

    const runSearch = async () => {
      const current = searchInput.value.trim();
      if (!searchUrl) {
        return;
      }
      if (!current) {
        searchList.innerHTML = '<li class="panel muted">请在上方输入单号片段开始搜索。</li>';
        latestQuery = '';
        return;
      }
      if (current === latestQuery) {
        return;
      }
      latestQuery = current;

      try {
        const response = await fetch(`${searchUrl}?q=${encodeURIComponent(current)}`, {
          credentials: 'same-origin',
        });
        if (!response.ok) {
          searchList.innerHTML = '<li class="panel muted">搜索失败，请稍后再试。</li>';
          return;
        }
        const payload = await response.json();
        renderSearchItems(payload.items);
      } catch (error) {
        console.error(error);
        searchList.innerHTML = '<li class="panel muted">搜索失败，请稍后再试。</li>';
      }
    };

    searchInput.addEventListener('input', () => {
      if (timer) {
        clearTimeout(timer);
      }
      timer = setTimeout(runSearch, 260);
    });
  }
})();
