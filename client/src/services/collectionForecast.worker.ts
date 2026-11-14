import { forecast, type ForecastInput } from './collectionForecast';

type WorkerRequest = ForecastInput | { id: number; input: ForecastInput };

self.onmessage = (event: MessageEvent<WorkerRequest>) => {
  const payload = 'input' in event.data ? event.data.input : event.data;
  const id = 'input' in event.data ? event.data.id : undefined;
  try {
    self.postMessage({ id, result: forecast(payload) });
  } catch (error) {
    self.postMessage({ id, error: String(error) });
  }
};
