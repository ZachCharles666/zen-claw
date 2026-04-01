/**
 * WhatsApp client wrapper using Baileys.
 * Based on OpenClaw's working implementation.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} from '@whiskeysockets/baileys';

import { Boom } from '@hapi/boom';
import { mkdir, writeFile } from 'fs/promises';
import { join } from 'path';
import qrcode from 'qrcode-terminal';
import pino from 'pino';

const VERSION = '0.1.0';

export interface InboundMessage {
  id: string;
  sender: string;
  pn: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  mediaType?: string;
  localMediaPath?: string;
}

export interface WhatsAppClientOptions {
  authDir: string;
  onMessage: (msg: InboundMessage) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string) => void;
}

export class WhatsAppClient {
  private sock: any = null;
  private options: WhatsAppClientOptions;
  private reconnecting = false;

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
  }

  async connect(): Promise<void> {
    const logger = pino({ level: 'silent' });
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const { version } = await fetchLatestBaileysVersion();
    this.options.onStatus('starting');

    console.log(`Using Baileys version: ${version.join('.')}`);

    // Create socket following OpenClaw's pattern
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false,
      browser: ['nano-claw', 'cli', VERSION],
      syncFullHistory: false,
      markOnlineOnConnect: false,
    });

    // Handle WebSocket errors
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // Handle connection updates
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Display QR code in terminal
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
        this.options.onStatus('qr_required');
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(`Connection closed. Status: ${statusCode}, Will reconnect: ${shouldReconnect}`);
        this.options.onStatus('disconnected');

        if (shouldReconnect && !this.reconnecting) {
          this.reconnecting = true;
          this.options.onStatus('reconnecting');
          console.log('Reconnecting in 5 seconds...');
          setTimeout(() => {
            this.reconnecting = false;
            this.connect();
          }, 5000);
        }
      } else if (connection === 'open') {
        console.log('�?Connected to WhatsApp');
        this.options.onStatus('connected');
      }
    });

    // Save credentials on update
    this.sock.ev.on('creds.update', saveCreds);

    // Handle incoming messages
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify') return;

      for (const msg of messages) {
        // Skip own messages
        if (msg.key.fromMe) continue;

        // Skip status updates
        if (msg.key.remoteJid === 'status@broadcast') continue;

        const inbound = await this.extractInboundMessage(msg);
        if (!inbound) continue;

        const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;

        this.options.onMessage({
          id: msg.key.id || '',
          sender: msg.key.remoteJid || '',
          pn: msg.key.remoteJidAlt || '',
          content: inbound.content,
          timestamp: msg.messageTimestamp as number,
          isGroup,
          mediaType: inbound.mediaType,
          localMediaPath: inbound.localMediaPath,
        });
      }
    });
  }

  private async extractInboundMessage(
    msg: any,
  ): Promise<Pick<InboundMessage, 'content' | 'mediaType' | 'localMediaPath'> | null> {
    const message = msg.message;
    if (!message) return null;

    // Text message
    if (message.conversation) {
      return { content: message.conversation };
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return { content: message.extendedTextMessage.text };
    }

    // Image with caption
    if (message.imageMessage?.caption) {
      return { content: `[Image] ${message.imageMessage.caption}` };
    }

    // Video with caption
    if (message.videoMessage?.caption) {
      return { content: `[Video] ${message.videoMessage.caption}` };
    }

    // Document with caption
    if (message.documentMessage?.caption) {
      return { content: `[Document] ${message.documentMessage.caption}` };
    }

    // Voice/Audio message
    if (message.audioMessage) {
      return {
        content: '[Voice Message]',
        mediaType: 'audio',
        localMediaPath: await this.downloadAudioMessage(msg),
      };
    }

    return null;
  }

  private async downloadAudioMessage(msg: any): Promise<string> {
    if (!this.sock) return '';
    try {
      const audio = msg.message?.audioMessage;
      if (!audio) return '';
      const mediaDir = join(this.options.authDir, 'media');
      await mkdir(mediaDir, { recursive: true });
      const ext = this.resolveAudioExtension(audio.mimetype);
      const messageId = String(msg.key?.id || Date.now());
      const fileName = `whatsapp_${messageId}${ext}`;
      const outputPath = join(mediaDir, fileName);

      const media = await downloadMediaMessage(
        msg,
        'buffer',
        {},
        {
          logger: pino({ level: 'silent' }),
          reuploadRequest: this.sock.updateMediaMessage,
        } as any,
      );
      if (!media) return '';
      const buffer = Buffer.isBuffer(media) ? media : Buffer.from(media as Uint8Array);
      await writeFile(outputPath, buffer);
      return outputPath;
    } catch (error) {
      console.warn('Audio download failed:', error);
      this.options.onStatus('audio_download_failed');
      return '';
    }
  }

  private resolveAudioExtension(mimeType?: string): string {
    const normalized = String(mimeType || '').toLowerCase();
    if (normalized.includes('ogg')) return '.ogg';
    if (normalized.includes('mpeg')) return '.mp3';
    if (normalized.includes('mp4') || normalized.includes('m4a')) return '.m4a';
    if (normalized.includes('amr')) return '.amr';
    if (normalized.includes('wav')) return '.wav';
    if (normalized.includes('webm')) return '.webm';
    return '.bin';
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    await this.sock.sendMessage(to, { text });
  }

  async disconnect(): Promise<void> {
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}


