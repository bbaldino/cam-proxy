class DoorbellCard extends HTMLElement {
  setConfig(config) {
    this._config = config;
  }

  connectedCallback() {
    const url = this._config.url || '/webrtc-doorbell.html';
    const height = this._config.height || '600px';

    this.innerHTML = `
      <ha-card style="overflow: hidden;">
        <iframe
          src="${url}?v=${Date.now()}"
          allow="microphone; autoplay"
          style="width: 100%; height: ${height}; border: none; display: block;"
        ></iframe>
      </ha-card>
    `;
  }

  getCardSize() {
    return 5;
  }

  static getStubConfig() {
    return { url: '/webrtc-doorbell.html' };
  }
}

customElements.define('doorbell-card', DoorbellCard);
