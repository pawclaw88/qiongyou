// config.js
const PRODUCTION_API_BASE = 'https://routehop.quest';
const DEVELOPMENT_API_BASE = 'http://localhost:8000'; // Only for local testing

// Automatically switch based on environment (or use a manual flag)
const isProduction = true; // Set to false for development

export const API_BASE_URL = isProduction ? PRODUCTION_API_BASE : DEVELOPMENT_API_BASE;