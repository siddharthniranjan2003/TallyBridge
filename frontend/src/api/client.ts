import axios from "axios";

const BASE = import.meta.env.VITE_BACKEND_URL;
const KEY = import.meta.env.VITE_API_KEY;
export const COMPANY = import.meta.env.VITE_COMPANY;

export const api = axios.create({
    baseURL: BASE,
    headers: { "x-api-key": KEY },
});

export const get = (url: string, params = {}) =>
    api.get(url, { params: { company_name: COMPANY, ...params } })
        .then(r => r.data);

export const fmt = (n: number) =>
    "₹" + new Intl.NumberFormat("en-IN").format(Math.round(Math.abs(n || 0)));