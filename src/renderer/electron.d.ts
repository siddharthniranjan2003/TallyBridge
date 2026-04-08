export {};

type TallyCompanySelection = {
  name: string;
  guid?: string;
  formalName?: string;
};

declare global {
  interface Window {
    electronAPI: {
      getConfig: () => Promise<any>;
      saveSettings: (s: any) => Promise<any>;
      addCompany: (selection: TallyCompanySelection) => Promise<{ success: boolean; error?: string }>;
      removeCompany: (id: string) => Promise<any>;
      getCompanies: () => Promise<any[]>;
      syncNow: () => Promise<any>;
      checkTally: () => Promise<{ connected: boolean }>;
      checkTallyCapabilities: () => Promise<any>;
      getTallyCompanies: () => Promise<{ success: boolean; companies: TallyCompanySelection[] }>;
      on: (channel: string, cb: (...args: any[]) => void) => void;
      off: (channel: string, cb: (...args: any[]) => void) => void;
    };
  }
}
