import React, { useEffect, useState } from 'react';

interface AuditLog {
  log_id: string;
  timestamp: string;
  event_type: string;
  user_id: string | null;
  case_id: string | null;
  details: Record<string, any>;
}

export const AuditLogPage: React.FC = () => {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  
  // Filters
  const [eventType, setEventType] = useState('');
  const [caseId, setCaseId] = useState('');
  const [userId, setUserId] = useState('');

  const fetchLogs = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (eventType) params.append('event_type', eventType);
      if (caseId) params.append('case_id', caseId);
      if (userId) params.append('user_id', userId);

      const res = await fetch(`/api/admin/audit-logs?${params.toString()}`);
      if (!res.ok) {
        throw new Error(`Failed to fetch logs: ${res.statusText}`);
      }
      const data = await res.json();
      setLogs(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs();
  }, [eventType, caseId, userId]);

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Audit Logs (Chain of Custody)</h1>
        <button
          onClick={fetchLogs}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          Refresh
        </button>
      </div>

      {error && (
        <div className="p-4 bg-red-100 text-red-700 rounded-md">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <input
          type="text"
          placeholder="Filter by Event Type"
          value={eventType}
          onChange={(e) => setEventType(e.target.value)}
          className="p-2 border rounded dark:bg-gray-800 dark:border-gray-700 dark:text-white"
        />
        <input
          type="text"
          placeholder="Filter by Case ID"
          value={caseId}
          onChange={(e) => setCaseId(e.target.value)}
          className="p-2 border rounded dark:bg-gray-800 dark:border-gray-700 dark:text-white"
        />
        <input
          type="text"
          placeholder="Filter by User ID"
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          className="p-2 border rounded dark:bg-gray-800 dark:border-gray-700 dark:text-white"
        />
      </div>

      <div className="bg-white dark:bg-gray-800 shadow rounded-lg overflow-hidden">
        {loading ? (
          <div className="p-6 text-center text-gray-500">Loading logs...</div>
        ) : logs.length === 0 ? (
          <div className="p-6 text-center text-gray-500">No logs found matching criteria.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-900">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Timestamp</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Event Type</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Case / User</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Details</th>
                </tr>
              </thead>
              <tbody className="bg-white dark:bg-gray-800 divide-y divide-gray-200 dark:divide-gray-700">
                {logs.map((log) => (
                  <tr key={log.log_id}>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900 dark:text-gray-300">
                      {new Date(log.timestamp).toLocaleString()}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span className={`px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${
                        log.event_type === 'authorization_violation' ? 'bg-red-100 text-red-800' :
                        log.event_type.startsWith('admin_') ? 'bg-purple-100 text-purple-800' :
                        'bg-blue-100 text-blue-800'
                      }`}>
                        {log.event_type}
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 dark:text-gray-400">
                      <div><span className="font-semibold text-gray-700 dark:text-gray-300">Case:</span> {log.case_id || 'N/A'}</div>
                      <div className="mt-1"><span className="font-semibold text-gray-700 dark:text-gray-300">User:</span> {log.user_id ? log.user_id.substring(0,8) + '...' : 'System'}</div>
                    </td>
                    <td className="px-6 py-4 text-sm text-gray-500 dark:text-gray-400">
                      <pre className="text-xs max-h-32 overflow-y-auto bg-gray-50 dark:bg-gray-900 p-2 rounded">
                        {JSON.stringify(log.details, null, 2)}
                      </pre>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

export default AuditLogPage;
