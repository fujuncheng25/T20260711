(() => {
  const QUEUE_DB_NAME = 'tmall-scrabber-local-queue';
  const QUEUE_STORE_NAME = 'captures';
  const QUEUE_DB_VERSION = 1;

  const stageLabels = {
    queued: '已缓存',
    preparing: '本地识别中',
    initUploading: '上传缩略图中',
    initDone: '已提交单号和缩略图',
    originalUploading: '上传原图中',
    completed: '已完成',
    failed: '上传失败，等待重试',
  };

  const escapeHtml = (text) => String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

  const localTimeFormatters = {
    time: new Intl.DateTimeFormat(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }),
    datetime: new Intl.DateTimeFormat(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    }),
  };

  const parseUtcMoment = (value) => {
    const raw = String(value || '').trim();
    if (!raw) {
      return null;
    }

    const hasTimezone = /([zZ]|[+-]\d{2}:\d{2})$/.test(raw);
    const normalized = hasTimezone ? raw : `${raw}Z`;
    const moment = new Date(normalized);
    if (Number.isNaN(moment.getTime())) {
      return null;
    }
    return moment;
  };

  const formatLocalTime = (value, mode = 'datetime', fallback = '') => {
    const moment = parseUtcMoment(value);
    if (!moment) {
      return String(fallback || '');
    }

    const formatter = localTimeFormatters[mode] || localTimeFormatters.datetime;
    return formatter.format(moment);
  };

  const renderLocalTimes = (root = document) => {
    const nodes = root.querySelectorAll('.js-local-time[data-utc]');
    nodes.forEach((node) => {
      const mode = node.dataset.timeFormat || 'datetime';
      const fallback = node.textContent || '';
      node.textContent = formatLocalTime(node.dataset.utc, mode, fallback);
    });
  };

  const sleep = (ms) => new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

  const fileToArrayBuffer = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error('文件读取失败'));
    reader.readAsArrayBuffer(file);
  });

  const arrayBufferToDataUrl = (buffer, mimeType) => {
    const bytes = new Uint8Array(buffer);
    let binary = '';
    for (let index = 0; index < bytes.byteLength; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return `data:${mimeType};base64,${btoa(binary)}`;
  };

  const toBlobFromDataUrl = (dataUrl) => {
    const parts = String(dataUrl).split(',');
    if (parts.length !== 2) {
      throw new Error('data url 无效');
    }
    const mimeMatch = parts[0].match(/data:(.*?);base64/i);
    const mimeType = mimeMatch ? mimeMatch[1] : 'image/jpeg';
    const raw = atob(parts[1]);
    const buffer = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) {
      buffer[index] = raw.charCodeAt(index);
    }
    return new Blob([buffer], { type: mimeType });
  };

  const openQueueDb = () => new Promise((resolve, reject) => {
    const request = indexedDB.open(QUEUE_DB_NAME, QUEUE_DB_VERSION);

    request.onupgradeneeded = (event) => {
      const db = event.target.result;
      if (!db.objectStoreNames.contains(QUEUE_STORE_NAME)) {
        const store = db.createObjectStore(QUEUE_STORE_NAME, { keyPath: 'id' });
        store.createIndex('createdAt', 'createdAt', { unique: false });
        store.createIndex('stage', 'stage', { unique: false });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('打开本地缓存失败'));
  });

  const withStore = async (mode, handler) => {
    const db = await openQueueDb();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(QUEUE_STORE_NAME, mode);
        const store = tx.objectStore(QUEUE_STORE_NAME);
        let settled = false;
        const safeResolve = (value) => {
          if (settled) {
            return;
          }
          settled = true;
          resolve(value);
        };
        const safeReject = (error) => {
          if (settled) {
            return;
          }
          settled = true;
          reject(error);
        };

        tx.oncomplete = () => safeResolve(undefined);
        tx.onerror = () => safeReject(tx.error || new Error('本地缓存事务失败'));
        tx.onabort = () => safeReject(tx.error || new Error('本地缓存事务终止'));

        try {
          handler(store, tx, safeResolve, safeReject);
        } catch (error) {
          safeReject(error);
        }
      });
    } finally {
      db.close();
    }
  };

  const putQueueItem = (item) => withStore('readwrite', (store) => {
    store.put(item);
  });

  const getQueueItem = (id) => withStore('readonly', (store, tx, resolve) => {
    const request = store.get(id);
    request.onsuccess = () => resolve(request.result || null);
  });

  const getAllQueueItems = () => withStore('readonly', (store, tx, resolve) => {
    const request = store.getAll();
    request.onsuccess = () => resolve(Array.isArray(request.result) ? request.result : []);
  });

  const deleteQueueItem = (id) => withStore('readwrite', (store) => {
    store.delete(id);
  });

  const updateQueueItem = async (id, patch) => {
    const item = await getQueueItem(id);
    if (!item) {
      return null;
    }
    const next = {
      ...item,
      ...patch,
      updatedAt: Date.now(),
    };
    await putQueueItem(next);
    return next;
  };

  const ensureQueueItem = (raw) => ({
    attempts: 0,
    stage: 'queued',
    recognizedCandidates: [],
    recognizedTextLines: [],
    recognizedOrderNo: '',
    localMatchHint: null,
    initResponse: null,
    pickupLogId: null,
    ...raw,
  });

  const drawImageFromBlob = (blob) => new Promise((resolve, reject) => {
    const image = new Image();
    const objectUrl = URL.createObjectURL(blob);
    image.onload = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(image);
    };
    image.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error('图片解码失败'));
    };
    image.src = objectUrl;
  });

  const renderDataUrlToCanvas = async (dataUrl) => {
    const blob = toBlobFromDataUrl(dataUrl);
    const image = await drawImageFromBlob(blob);
    const canvas = document.createElement('canvas');
    canvas.width = image.width;
    canvas.height = image.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      throw new Error('无法创建画布');
    }
    ctx.drawImage(image, 0, 0);
    return { canvas, ctx };
  };

  const getCanvasBlob = (canvas, mimeType, quality) => new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error('图片压缩失败'));
        return;
      }
      resolve(blob);
    }, mimeType, quality);
  });

  const compressImageDataUrl = async (sourceDataUrl, targetBytes = 2048) => {
    const { canvas } = await renderDataUrlToCanvas(sourceDataUrl);
    const maxSide = Math.max(canvas.width, canvas.height);
    const scale = maxSide > 900 ? 900 / maxSide : 1;

    let workingCanvas = document.createElement('canvas');
    workingCanvas.width = Math.max(1, Math.round(canvas.width * scale));
    workingCanvas.height = Math.max(1, Math.round(canvas.height * scale));

    let workingCtx = workingCanvas.getContext('2d');
    if (!workingCtx) {
      throw new Error('无法创建输出画布');
    }
    workingCtx.drawImage(canvas, 0, 0, workingCanvas.width, workingCanvas.height);

    let quality = 0.68;
    let blob = await getCanvasBlob(workingCanvas, 'image/jpeg', quality);

    for (let round = 0; round < 14 && blob.size > targetBytes; round += 1) {
      quality = Math.max(0.04, quality * 0.82);
      blob = await getCanvasBlob(workingCanvas, 'image/jpeg', quality);
      if (blob.size <= targetBytes) {
        break;
      }

      if (round % 4 === 3 && Math.min(workingCanvas.width, workingCanvas.height) > 120) {
        const resizedCanvas = document.createElement('canvas');
        resizedCanvas.width = Math.max(120, Math.round(workingCanvas.width * 0.86));
        resizedCanvas.height = Math.max(120, Math.round(workingCanvas.height * 0.86));
        const resizedCtx = resizedCanvas.getContext('2d');
        if (!resizedCtx) {
          break;
        }
        resizedCtx.drawImage(workingCanvas, 0, 0, resizedCanvas.width, resizedCanvas.height);
        workingCanvas = resizedCanvas;
        workingCtx = workingCanvas.getContext('2d');
        if (!workingCtx) {
          break;
        }
        blob = await getCanvasBlob(workingCanvas, 'image/jpeg', quality);
      }
    }

    if (blob.size > targetBytes) {
      const tinyCanvas = document.createElement('canvas');
      tinyCanvas.width = 56;
      tinyCanvas.height = 56;
      const tinyCtx = tinyCanvas.getContext('2d');
      if (!tinyCtx) {
        throw new Error('无法压缩缩略图');
      }
      tinyCtx.fillStyle = '#f3f3f3';
      tinyCtx.fillRect(0, 0, 56, 56);
      tinyCtx.drawImage(workingCanvas, 0, 0, 56, 56);
      blob = await getCanvasBlob(tinyCanvas, 'image/jpeg', 0.28);
    }

    const buffer = await blob.arrayBuffer();
    return arrayBufferToDataUrl(buffer, 'image/jpeg');
  };

  const tokenizeCandidates = (rawText) => {
    const cleaned = String(rawText || '').toUpperCase().replace(/[^A-Z0-9\n]/g, ' ');
    const map = {
      O: '0', D: '0', Q: '0', I: '1', L: '1', Z: '2', S: '5', B: '8',
    };
    const candidates = new Set();

    const parts = cleaned.split(/\s+/).filter((part) => part.length >= 8);
    parts.forEach((part) => {
      const base = part.replace(/[^A-Z0-9]/g, '');
      if (!base || base.length < 8) {
        return;
      }
      const mapped = base.split('').map((char) => map[char] || char).join('');
      [base, mapped].forEach((value) => {
        const digits = value.replace(/\D/g, '');
        if (value.length >= 8 && digits.length >= 6) {
          candidates.add(value);
        }
      });
    });

    return Array.from(candidates).sort((left, right) => right.length - left.length || left.localeCompare(right));
  };

  const scanBarcodesLocally = async (imageBlob) => {
    if (!(window.BarcodeDetector && typeof window.BarcodeDetector === 'function')) {
      return [];
    }

    try {
      const preferredFormats = [
        'code_128',
        'code_39',
        'ean_13',
        'ean_8',
        'upc_a',
        'upc_e',
        'itf',
        'codabar',
      ];

      let detector;
      if (typeof window.BarcodeDetector.getSupportedFormats === 'function') {
        const supported = await window.BarcodeDetector.getSupportedFormats();
        const formats = preferredFormats.filter((item) => supported.includes(item));
        detector = formats.length > 0
          ? new window.BarcodeDetector({ formats })
          : new window.BarcodeDetector();
      } else {
        detector = new window.BarcodeDetector();
      }

      const bitmap = await createImageBitmap(imageBlob);
      try {
        const rows = await detector.detect(bitmap);
        const values = rows
          .map((row) => String(row.rawValue || '').trim())
          .filter((row) => row.length > 0);
        return Array.from(new Set(values));
      } finally {
        if (typeof bitmap.close === 'function') {
          bitmap.close();
        }
      }
    } catch (error) {
      console.error(error);
      return [];
    }
  };

  const localRecognizeOrderNumbers = async (originalDataUrl) => {
    const imageBlob = toBlobFromDataUrl(originalDataUrl);
    const lines = await scanBarcodesLocally(imageBlob);
    const candidates = tokenizeCandidates(lines.join('\n'));
    return { lines, candidates };
  };

  const extractDigits = (value) => String(value || '').replace(/\D/g, '');

  const localSuffixMatches = (candidate, suffix) => {
    const normalizedCandidate = String(candidate || '').trim().toUpperCase();
    const normalizedSuffix = String(suffix || '').trim();
    if (!normalizedCandidate || !normalizedSuffix) {
      return false;
    }

    if (normalizedCandidate.endsWith(normalizedSuffix)) {
      return true;
    }

    const suffixDigits = extractDigits(normalizedSuffix);
    if (suffixDigits.length < 4) {
      return false;
    }
    return extractDigits(normalizedCandidate).endsWith(suffixDigits);
  };

  const parseFrontendReminderSnapshot = () => {
    const node = document.getElementById('frontend-reminder-snapshot');
    if (!node) {
      return [];
    }

    try {
      const parsed = JSON.parse(node.textContent || '[]');
      if (!Array.isArray(parsed)) {
        return [];
      }

      return parsed
        .map((row) => {
          const watcherName = String((row && row.watcher_name) || '').trim();
          const orderSuffix = extractDigits((row && row.order_suffix) || '');
          const groupNames = Array.isArray(row && row.group_names)
            ? row.group_names
              .map((groupName) => String(groupName || '').trim())
              .filter((groupName) => groupName.length > 0)
            : [];

          return {
            watcherName,
            orderSuffix,
            groupNames,
          };
        })
        .filter((row) => row.watcherName.length > 0 && row.orderSuffix.length >= 4);
    } catch (error) {
      console.error(error);
      return [];
    }
  };

  const frontendReminderSnapshot = parseFrontendReminderSnapshot();

  const pickFrontendMatchHint = (candidates) => {
    if (!Array.isArray(candidates) || candidates.length === 0) {
      return null;
    }
    if (frontendReminderSnapshot.length === 0) {
      return null;
    }

    const hits = [];
    frontendReminderSnapshot.forEach((rule) => {
      const matchedCandidate = candidates.find((candidate) => localSuffixMatches(candidate, rule.orderSuffix));
      if (!matchedCandidate) {
        return;
      }

      const groupCandidates = rule.groupNames.length > 0 ? rule.groupNames : ['个人'];
      groupCandidates.forEach((groupName) => {
        hits.push({
          watcherName: rule.watcherName,
          groupName,
          orderSuffix: rule.orderSuffix,
          candidate: String(matchedCandidate || ''),
        });
      });
    });

    if (hits.length === 0) {
      return null;
    }

    return hits[Math.floor(Math.random() * hits.length)];
  };

  const makeQueueCardHtml = (item) => {
    const stageLabel = stageLabels[item.stage] || item.stage;
    const candidates = Array.isArray(item.recognizedCandidates)
      ? item.recognizedCandidates.filter((row) => String(row || '').trim().length > 0)
      : [];
    const previewCandidates = candidates.slice(0, 3).map((row) => escapeHtml(row));
    const candidateSuffix = candidates.length > 3 ? ` 等${candidates.length}个` : '';
    const orderNo = previewCandidates.length > 0
      ? `，单号 ${previewCandidates.join(' / ')}${candidateSuffix}`
      : (item.recognizedOrderNo ? `，单号 ${escapeHtml(item.recognizedOrderNo)}` : '');
    const hint = item.localMatchHint && typeof item.localMatchHint === 'object'
      ? item.localMatchHint
      : null;
    const hintText = hint && hint.watcherName
      ? `，前端预判 ${escapeHtml(hint.groupName || '个人')} / ${escapeHtml(hint.watcherName)}`
      : '';
    const errorText = item.lastError ? `<span class="queue-error">${escapeHtml(item.lastError)}</span>` : '';
    return `
      <li class="queue-item queue-${escapeHtml(item.stage || 'queued')}">
        <strong>${escapeHtml(new Date(item.createdAt || Date.now()).toLocaleTimeString())}</strong>
        <span>${escapeHtml(stageLabel)}${orderNo}${hintText}</span>
        ${errorText}
      </li>
    `;
  };

  const renderQueue = async () => {
    const statusEl = document.getElementById('capture-queue-status');
    const listEl = document.getElementById('capture-queue-list');
    if (!statusEl || !listEl) {
      return;
    }

    const rows = (await getAllQueueItems()).sort((left, right) => Number(right.createdAt || 0) - Number(left.createdAt || 0));
    statusEl.textContent = `本地缓存队列：${rows.length} 张`;
    if (rows.length === 0) {
      listEl.innerHTML = '<li class="queue-item queue-empty">暂无任务</li>';
      return;
    }
    listEl.innerHTML = rows.slice(0, 8).map((item) => makeQueueCardHtml(ensureQueueItem(item))).join('');
  };

  const safeFetchJson = async (url, options) => {
    const response = await fetch(url, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      const message = (payload && payload.error) ? payload.error : `请求失败(${response.status})`;
      throw new Error(message);
    }
    return payload || {};
  };

  let queueWorkerActive = false;
  const processQueue = async (initUrl, finalizeUrl) => {
    if (queueWorkerActive) {
      return;
    }
    queueWorkerActive = true;

    try {
      while (true) {
        const allItems = (await getAllQueueItems())
          .map((item) => ensureQueueItem(item))
          .sort((left, right) => Number(left.createdAt || 0) - Number(right.createdAt || 0));

        const pending = allItems.find((item) => item.stage !== 'completed');
        if (!pending) {
          break;
        }

        const nextAttempts = Number(pending.attempts || 0) + 1;
        await updateQueueItem(pending.id, { attempts: nextAttempts, lastError: '' });

        try {
          let current = ensureQueueItem((await getQueueItem(pending.id)) || pending);

          if (current.stage === 'queued' || current.stage === 'failed') {
            await updateQueueItem(current.id, { stage: 'preparing' });
            current = ensureQueueItem((await getQueueItem(current.id)) || current);

            const localResult = await localRecognizeOrderNumbers(current.originalDataUrl);
            const recognizedCandidates = Array.isArray(localResult.candidates) ? localResult.candidates : [];
            const recognizedTextLines = Array.isArray(localResult.lines) ? localResult.lines : [];
            const recognizedOrderNo = recognizedCandidates[0] || '';
            const localMatchHint = pickFrontendMatchHint(recognizedCandidates);
            const thumbnailDataUrl = await compressImageDataUrl(current.originalDataUrl, 2048);

            await updateQueueItem(current.id, {
              stage: 'initUploading',
              thumbnailDataUrl,
              recognizedCandidates,
              recognizedTextLines,
              recognizedOrderNo,
              localMatchHint,
            });
            current = ensureQueueItem((await getQueueItem(current.id)) || current);

            const initPayload = await safeFetchJson(initUrl, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              credentials: 'same-origin',
              body: JSON.stringify({
                thumbnail_data_url: current.thumbnailDataUrl,
                recognized_order_no: current.recognizedOrderNo,
                recognized_candidates: current.recognizedCandidates,
                recognized_text_lines: current.recognizedTextLines,
              }),
            });

            const mergedCandidates = Array.isArray(initPayload.recognized_order_candidates)
              ? initPayload.recognized_order_candidates
              : current.recognizedCandidates;

            await updateQueueItem(current.id, {
              stage: 'initDone',
              initResponse: initPayload,
              pickupLogId: Number(initPayload.pickup_log_id || 0) || null,
              recognizedCandidates: mergedCandidates,
              recognizedOrderNo: String(initPayload.recognized_order_no || current.recognizedOrderNo || ''),
              localMatchHint: pickFrontendMatchHint(mergedCandidates),
            });
            current = ensureQueueItem((await getQueueItem(current.id)) || current);
          }

          if (current.stage === 'initDone' || current.stage === 'originalUploading') {
            await updateQueueItem(current.id, { stage: 'originalUploading' });
            current = ensureQueueItem((await getQueueItem(current.id)) || current);

            if (!current.pickupLogId) {
              throw new Error('缺少日志编号，无法补传原图');
            }

            const originalBlob = toBlobFromDataUrl(current.originalDataUrl);
            const formData = new FormData();
            formData.append('pickup_log_id', String(current.pickupLogId));
            formData.append('pickup_image', originalBlob, current.originalName || `capture_${current.id}.jpg`);

            await safeFetchJson(finalizeUrl, {
              method: 'POST',
              credentials: 'same-origin',
              body: formData,
            });

            await updateQueueItem(current.id, { stage: 'completed', lastError: '' });
            await sleep(1200);
            await deleteQueueItem(current.id);
          }
        } catch (error) {
          await updateQueueItem(pending.id, {
            stage: 'failed',
            lastError: error && error.message ? String(error.message) : '上传失败',
          });
          await sleep(Math.min(15000, 1200 * nextAttempts));
        }

        await renderQueue();
      }
    } finally {
      queueWorkerActive = false;
      await renderQueue();
    }
  };

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

  renderLocalTimes();

  const cameraButton = document.getElementById('camera-button');
  const pickupInput = document.getElementById('pickup-image-input');
  const cameraForm = document.getElementById('camera-form');

  if (cameraButton && pickupInput && cameraForm && 'indexedDB' in window) {
    const stagedInitUrl = cameraForm.dataset.stagedInitUrl || '';
    const stagedFinalizeUrl = cameraForm.dataset.stagedFinalizeUrl || '';

    cameraButton.addEventListener('click', () => {
      pickupInput.click();
    });

    const queueIncomingFiles = async (files) => {
      const now = Date.now();
      for (let index = 0; index < files.length; index += 1) {
        const file = files[index];
        if (!file) {
          continue;
        }
        const fileBuffer = await fileToArrayBuffer(file);
        const originalDataUrl = arrayBufferToDataUrl(fileBuffer, file.type || 'image/jpeg');
        const item = ensureQueueItem({
          id: `${now}-${index}-${Math.random().toString(36).slice(2, 8)}`,
          createdAt: Date.now(),
          updatedAt: Date.now(),
          stage: 'queued',
          originalName: file.name || `capture_${now}.jpg`,
          originalType: file.type || 'image/jpeg',
          originalDataUrl,
          size: file.size || 0,
        });
        await putQueueItem(item);
      }

      await renderQueue();
      if (stagedInitUrl && stagedFinalizeUrl) {
        processQueue(stagedInitUrl, stagedFinalizeUrl);
      }
    };

    pickupInput.addEventListener('change', async () => {
      if (pickupInput.files && pickupInput.files.length > 0) {
        cameraButton.disabled = true;
        try {
          await queueIncomingFiles(Array.from(pickupInput.files));
        } catch (error) {
          console.error(error);
        } finally {
          pickupInput.value = '';
          cameraButton.disabled = false;
        }
      }
    });

    renderQueue().then(() => {
      if (stagedInitUrl && stagedFinalizeUrl) {
        processQueue(stagedInitUrl, stagedFinalizeUrl);
      }
    });
  } else if (cameraButton && pickupInput && cameraForm) {
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
  const readApiPrefix = (pollAnchor && pollAnchor.dataset.readApiPrefix) ? pollAnchor.dataset.readApiPrefix : '/api/notifications/';

  const toNotificationId = (value) => {
    const parsed = Number(value || 0);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  };

  const buildReadApiUrl = (notificationId) => `${readApiPrefix}${encodeURIComponent(String(notificationId))}/read`;

  const ensureMessagePlaceholder = () => {
    if (!messageBar || (messageList && messageList.children.length > 0)) {
      return;
    }

    let emptyText = messageBar.querySelector('.message-bar-empty');
    if (!emptyText) {
      emptyText = document.createElement('p');
      emptyText.className = 'muted message-bar-empty';
      emptyText.textContent = '暂无到货提醒。';
      messageBar.appendChild(emptyText);
    }
  };

  const clearMessagePlaceholder = () => {
    if (!messageBar) {
      return;
    }
    const emptyText = messageBar.querySelector('.message-bar-empty, .muted');
    if (emptyText && emptyText.closest('.message-bar') === messageBar && !emptyText.closest('.msg-item')) {
      emptyText.remove();
    }
  };

  const syncNotificationCardReadState = (notificationId) => {
    const selector = `.notes li[data-notification-id="${notificationId}"]`;
    const card = document.querySelector(selector);
    if (!card) {
      return;
    }
    card.classList.remove('unread');
    const form = card.querySelector('.js-mark-read-form');
    if (form) {
      form.remove();
    }
  };

  const removeMessageRowById = (notificationId) => {
    if (!messageList || !notificationId) {
      return;
    }
    const row = messageList.querySelector(`.msg-item[data-notification-id="${notificationId}"]`);
    if (row) {
      row.remove();
    }
    ensureMessagePlaceholder();
  };

  const markNotificationRead = async (notificationId) => {
    const normalizedId = toNotificationId(notificationId);
    if (!normalizedId) {
      return false;
    }

    try {
      const payload = await safeFetchJson(buildReadApiUrl(normalizedId), {
        method: 'POST',
        credentials: 'same-origin',
      });
      if (!payload.ok) {
        return false;
      }

      syncNotificationCardReadState(normalizedId);
      removeMessageRowById(normalizedId);
      return true;
    } catch (error) {
      console.error(error);
      return false;
    }
  };

  const closeSwipedRows = (exceptRow = null) => {
    if (!messageList) {
      return;
    }
    messageList.querySelectorAll('.msg-item.is-swiped').forEach((row) => {
      if (row !== exceptRow) {
        row.classList.remove('is-swiped');
      }
    });
  };

  const bindMessageRowInteractions = (row) => {
    if (!row || row.dataset.swipeBound === '1') {
      return;
    }
    row.dataset.swipeBound = '1';

    const deleteButton = row.querySelector('.msg-delete-btn');
    const swipeThreshold = 36;
    let startX = 0;
    let lastX = 0;
    let tracking = false;

    row.addEventListener('touchstart', (event) => {
      if (event.touches.length !== 1) {
        return;
      }
      startX = event.touches[0].clientX;
      lastX = startX;
      tracking = true;
    }, { passive: true });

    row.addEventListener('touchmove', (event) => {
      if (!tracking || event.touches.length !== 1) {
        return;
      }
      lastX = event.touches[0].clientX;
      if (lastX - startX < -8) {
        event.preventDefault();
      }
    }, { passive: false });

    row.addEventListener('touchend', () => {
      if (!tracking) {
        return;
      }
      tracking = false;
      const delta = lastX - startX;
      if (delta <= -swipeThreshold) {
        closeSwipedRows(row);
        row.classList.add('is-swiped');
      } else if (delta >= swipeThreshold) {
        row.classList.remove('is-swiped');
      }
    });

    row.addEventListener('click', (event) => {
      if (event.target.closest('.msg-delete-btn')) {
        return;
      }
      if (row.classList.contains('is-swiped')) {
        row.classList.remove('is-swiped');
        event.preventDefault();
        return;
      }
      closeSwipedRows(row);
    });

    if (deleteButton) {
      deleteButton.addEventListener('click', async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const notificationId = toNotificationId(row.dataset.notificationId);
        const removed = await markNotificationRead(notificationId);
        if (!removed) {
          row.classList.remove('is-swiped');
        }
      });
    }
  };

  const createMessageRow = (item) => {
    const notificationId = toNotificationId(item.id);
    const localTime = formatLocalTime(item.created_at_utc || item.created_at, 'time', item.created_at || '--:--');

    const row = document.createElement('li');
    row.className = 'msg-item';
    if (notificationId) {
      row.dataset.notificationId = String(notificationId);
    }

    const main = document.createElement('div');
    main.className = 'msg-item-main';

    const strong = document.createElement('strong');
    strong.textContent = localTime;
    main.appendChild(strong);

    const span = document.createElement('span');
    span.textContent = String(item.title || '快递到了');
    main.appendChild(span);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'msg-delete-btn';
    button.textContent = '删除';
    button.setAttribute('aria-label', '左滑删除消息');

    row.appendChild(main);
    row.appendChild(button);
    bindMessageRowInteractions(row);
    return row;
  };

  const bindMarkReadForms = () => {
    document.querySelectorAll('.js-mark-read-form').forEach((form) => {
      if (form.dataset.bound === '1') {
        return;
      }
      form.dataset.bound = '1';
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const notificationId = toNotificationId(form.dataset.notificationId);
        const done = await markNotificationRead(notificationId);
        if (!done) {
          form.submit();
        }
      });
    });
  };

  if (messageList) {
    messageList.querySelectorAll('.msg-item').forEach((row) => bindMessageRowInteractions(row));
  }
  bindMarkReadForms();

  if (pollAnchor) {
    const pollUrl = pollAnchor.dataset.pollUrl || '';
    const isBandwidthSaver = pollAnchor.dataset.bandwidthSaver === '1';
    let latestId = Number(pollAnchor.dataset.initialId || '0');

    const refreshNotifyButton = () => {
      if (!notifyButton) {
        return;
      }
      if (isBandwidthSaver) {
        notifyButton.textContent = '省流模式已开启（自动通知轮询已关闭）';
        notifyButton.disabled = true;
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

    if (!isBandwidthSaver && notifyButton && 'Notification' in window) {
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
        clearMessagePlaceholder();
        messageList = document.createElement('ul');
        messageList.className = 'msg-list';
        messageBar.appendChild(messageList);
      }
      if (!messageList) {
        return;
      }

      const notificationId = toNotificationId(item.id);
      if (notificationId) {
        const exists = messageList.querySelector(`.msg-item[data-notification-id="${notificationId}"]`);
        if (exists) {
          return;
        }
      }

      const row = createMessageRow(item);
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

    if (!isBandwidthSaver) {
      setInterval(pollNotifications, 15000);
    }
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
        const localTime = formatLocalTime(item.created_at_utc || item.created_at, 'datetime', item.created_at || '');
        return `
          <li class="panel">
            <p><strong>单号：${escapeHtml(item.order_no || '')}</strong></p>
            <p class="muted">LCS=${escapeHtml(item.lcs || 0)}，匹配度=${score.toFixed(2)}%</p>
            <p class="muted">拍照人：${escapeHtml(item.uploader_name || '')}，时间：${escapeHtml(localTime)}</p>
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
