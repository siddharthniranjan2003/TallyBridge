export {};

declare global {
  interface Window {
    electronAPI: {
      getConfig: () => Promise<any>;
      saveSettings: (s: any) => Promise<any>;
      addCompany: (name: string) => Promise<{ success: boolean; error?: string }>;
      removeCompany: (id: string) => Promise<any>;
      getCompanies: () => Promise<any[]>;
      syncNow: () => Promise<any>;
      checkTally: () => Promise<{ connected: boolean }>;
      getTallyCompanies: () => Promise<{ success: boolean; companies: string[] }>;
      on: (channel: string, cb: (...args: any[]) => void) => void;
      off: (channel: string, cb: (...args: any[]) => void) => void;
    };
  }
}