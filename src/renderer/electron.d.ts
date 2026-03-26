export {};

declare global {
  interface Window {
    electronAPI: {
      getConfig: () => Promise<any>;
      saveSettings: (s: any) => Promise<any>;
      addCompany: (name: string) => Promise<{ success: boolean; error?: string; company?: any }>;
      removeCompany: (id: string) => Promise<any>;
      getCompanies: () => Promise<any[]>;
      syncNow: () => Promise<any>;
      checkTally: () => Promise<{ connected: boolean }>;
      on: (channel: string, cb: (...args: any[]) => void) => void;
      off: (channel: string, cb: (...args: any[]) => void) => void;
    };
  }
}