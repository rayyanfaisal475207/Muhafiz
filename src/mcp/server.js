import { createPostgresMcp } from '../../mcp-servers/node_modules/postgres-mcp/dist/index.js';

const dbUrl = process.env.DB_MAIN_URL;
if (!dbUrl) {
    console.error("No DB_MAIN_URL provided");
    process.exit(1);
}

// Parse postgres connection string
const url = new URL(dbUrl);

const postgresMcp = createPostgresMcp({
  databaseConfigs: {
    main: {
      host: url.hostname,
      port: parseInt(url.port || '5432'),
      database: url.pathname.substring(1),
      user: url.username,
      password: url.password,
      ssl: { rejectUnauthorized: false }
    }
  },
  transport: 'stdio',
  autoStart: true
});

console.error("Started custom programmatic Postgres MCP server.");
