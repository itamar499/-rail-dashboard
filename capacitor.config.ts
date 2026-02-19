import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.itamar.raildashboard',
  appName: 'Rail Dashboard',
  webDir: 'web',
  server: {
    url: 'https://rail-dashboard.onrender.com',
    cleartext: false
  }
};

export default config;
