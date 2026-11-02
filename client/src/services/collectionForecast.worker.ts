import { forecast, type ForecastInput } from './collectionForecast';

self.onmessage = (event: MessageEvent<ForecastInput>) => {
  try {
    self.postMessage({ result: forecast(event.data) });
  } catch (error) {
    self.postMessage({ error: String(error) });
  }
};
