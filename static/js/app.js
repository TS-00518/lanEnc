/**
 * UploadManager Class
 * Encapsulates all file upload logic including progress bars and cancellations.
 */
class UploadManager {
    constructor(fileInputId, containerId, templateId) {
        this.fileInput = document.getElementById(fileInputId);
        this.container = document.getElementById(containerId);
        this.template = document.getElementById(templateId);

        if (this.fileInput) {
            this.fileInput.addEventListener('change', (e) => this.handleFiles(e.target.files));
        }
    }

    handleFiles(fileList) {
        if (!fileList.length) return;
        Array.from(fileList).forEach(file => this.uploadFile(file));
        this.fileInput.value = ''; // Reset input
    }

    uploadFile(file) {
        const clone = this.template.content.cloneNode(true);
        const el = clone.querySelector('.item-card');
        const id = 'upload-' + Math.random().toString(36).substr(2, 9);
        el.id = id;

        el.querySelector('.file-name-display').textContent = file.name;
        el.querySelector('.file-name-display').title = file.name;

        this.container.appendChild(el);
        const domEl = document.getElementById(id);

        const xhr = new XMLHttpRequest();
        const formData = new FormData();
        formData.append('files', file);

        // Progress Listener
        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const percent = (e.loaded / e.total) * 100;
                domEl.querySelector('.progress-bar').style.width = percent + '%';
                domEl.querySelector('.progress-text').textContent = Math.round(percent) + '%';
            }
        });

        // Load Listener
        xhr.addEventListener('load', () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                this.animateRemoval(domEl, () => {
                    htmx.trigger('body', 'update-files');
                });
            } else {
                this.setErrorState(domEl, "Failed");
            }
        });

        // Error Listener
        xhr.addEventListener('error', () => this.setErrorState(domEl, "Error"));

        // Cancel Listener
        const cancelBtn = domEl.querySelector('.cancel-btn');
        cancelBtn.onclick = () => {
            xhr.abort();
            this.animateRemoval(domEl, null, true);
        };

        xhr.open('POST', '/upload');
        xhr.send(formData);
    }

    setErrorState(el, msg) {
        const txt = el.querySelector('.progress-text');
        txt.textContent = msg;
        txt.classList.add('text-red-400');
    }

    animateRemoval(el, callback, isCancel = false) {
        if (isCancel) {
            // Flash effect for manual cancel
            el.style.transition = 'all 0.3s ease';
            el.style.backgroundColor = 'rgba(255,255,255,0.2)'; // Subtle flash

            setTimeout(() => {
                el.classList.add('fade-out-scale');
            }, 50);
        } else {
            el.classList.add('fade-out-up');
        }

        setTimeout(() => {
            if (el.parentNode) el.parentNode.removeChild(el);
            if (!isCancel && callback) callback();
        }, 350); // Matches CSS transition time + buffer
    }
}

/**
 * EventManager Class
 * Handles global event delegation and UI interactions.
 */
class EventManager {
    constructor() {
        this.initDelegation();
        this.initHtmxHooks();
    }

    initDelegation() {
        document.body.addEventListener('click', (e) => {
            const target = e.target;

            // Handle Refresh Button Rotation
            const refreshBtn = target.closest('#refresh-btn');
            if (refreshBtn) {
                this.rotateIcon(refreshBtn);
            }

            // Handle Queue Resume Animation Restart
            const resumeBtn = target.closest('[data-action="restart-anim"]');
            if (resumeBtn) {
                const row = resumeBtn.closest('.queue-card');
                if (row) {
                    row.classList.remove('animate-entry');
                    void row.offsetWidth; // Force reflow
                    row.classList.add('animate-entry');
                }
            }

            // Handle Flash Effect for "Add to Queue"
            const flashBtn = target.closest('[data-action="flash-card"]');
            if (flashBtn) {
                const card = flashBtn.closest('.item-card');
                if (card) {
                    card.classList.add('flash-white');
                    setTimeout(() => card.classList.remove('flash-white'), 500);
                }
            }
        });

        // Range Input Sync
        document.body.addEventListener('input', (e) => {
            if (e.target.classList.contains('range-sync')) {
                const output = e.target.previousElementSibling?.querySelector('output');
                if (output) output.value = e.target.value;
            }
        });
    }

    rotateIcon(btn) {
        const icon = btn.querySelector('i');
        if (!icon) return;
        const currentRot = parseInt(icon.dataset.rot || '0');
        const newRot = currentRot + 180;
        icon.dataset.rot = newRot;
        icon.style.transform = `rotate(${newRot}deg)`;
    }

    initHtmxHooks() {
        document.body.addEventListener('htmx:afterSwap', (evt) => {
            // Update Logo Energy State
            const processing = document.querySelector('[data-status="processing"]');
            const logo = document.getElementById("logo-enc");

            if (processing) {
                logo.classList.remove("text-transparent", "bg-clip-text", "bg-gradient-to-r", "from-indigo-400", "to-purple-400");
                logo.classList.add("logo-energy-active");
            } else {
                logo.classList.remove("logo-energy-active");
                logo.classList.add("text-transparent", "bg-clip-text", "bg-gradient-to-r", "from-indigo-400", "to-purple-400");
            }
        });
    }
}

// Initialize Application
document.addEventListener('DOMContentLoaded', () => {
    new UploadManager('file-upload-input', 'active-uploads', 'upload-card-template');
    new EventManager();
});
