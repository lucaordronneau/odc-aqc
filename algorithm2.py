import os
import gc
import sys
import json
import pickle

import pandas as pd
import numpy as np
import statsmodels.api as sm

import warnings
warnings.filterwarnings("ignore")

def get_input(local=False):
    if local:
        print("Reading local file dataset.csv")

        return "dataset.csv"

    dids = os.getenv("DIDS", None)

    if not dids:
        print("No DIDs found in environment. Aborting.")
        return

    dids = json.loads(dids)

    for did in dids:
        filename = f"data/inputs/{did}/0"  # 0 for metadata service
        print(f"Reading asset file {filename}.")

        return filename

def prepare_data(df):
    cols_hours = ['01h', '02h', '03h', '04h', '05h', '06h', '07h', '08h', '09h', '10h', 
                  '11h', '12h', '13h', '14h', '15h', '16h', '17h', '18h', '19h', '20h',
                  '21h', '22h', '23h', '24h']

    df = df[~(df['UNITATS'] == 'ppm')] # Don't have the molecular weight to convert into µg/m3
    
    # Conversion same unit
    df.loc[df['UNITATS'] == 'mg/m3', cols_hours] *= 1000
    df.loc[df['UNITATS'] == 'ng/m3', cols_hours] /= 1000
    
    df.loc[:, 'UNITATS'] = 'µg/m3'
    
    # Melt dataframe for treatment (series)
    df['DATA'] = pd.to_datetime(df['DATA'], format='%d/%m/%Y')

    chunk_size = len(df) // 50
    chunk_list = [pd.melt(df.iloc[i:i+chunk_size], id_vars=['CODI EOI', 'DATA', 'CONTAMINANT', 'TIPUS ESTACIO', 'AREA URBANA', 'ALTITUD'],
                    value_vars=cols_hours, var_name='hour', value_name='value').dropna() for i in range(0, len(df), chunk_size)]
    df = pd.concat(chunk_list, ignore_index=True)

    df['hour']  = df['hour'].str[:-1].astype(int)
    df['value'] = pd.to_numeric(df['value'], errors='coerce')
    df['datetime'] = df['DATA'] + pd.to_timedelta(df['hour'], unit='h')

    # POTENTIAL MEMORY CONSTRAINTS ON C2D

    df['month_year'] = df['datetime'].dt.strftime('%m-%Y')

    # Fill na with the mean of the hour of the same month and year from the same station (CODI EOI)
    df['value'].fillna(df.groupby(['CODI EOI', 'CONTAMINANT', 'hour', 'month_year'])['value'].transform('mean'), inplace=True)
    df.dropna(inplace=True)

    return df

def get_features(df):
    # Polluant NO
    df_NO = df[df['CONTAMINANT'] == 'NO']

    # Add feature AREA URBANA (mean of all polluant associated to AREA URBANA)
    pivot_df_area_urbana = pd.pivot_table(df[['datetime', 'value', 'AREA URBANA']], values='value', index='datetime', columns='AREA URBANA')
    df_area_urbana = pivot_df_area_urbana.groupby(pd.Grouper(freq="H")).mean()

    # Add feature AREA URBANA link to the polluant NO
    pivot_df_area_urbana_NO = pd.pivot_table(df_NO[['datetime', 'value', 'AREA URBANA']], values='value', index='datetime', columns='AREA URBANA')
    df_area_urbana_NO = pivot_df_area_urbana_NO.groupby(pd.Grouper(freq="H")).mean()
    df_area_urbana_NO = df_area_urbana_NO.rename(columns = {"urban":"urban_NO", "suburban":"suburban_NO", "rural":"rural_NO"})

    # Add the feature ALTITUD (all polluant)
    df_altitud = df[['datetime','ALTITUD']].groupby(pd.Grouper(key="datetime", freq="H")).mean()

    # Add the feature ALTITUD for the polluant NO
    df_altitud_NO = df_NO[['datetime', "ALTITUD"]].groupby(pd.Grouper(key="datetime", freq="H")).mean()
    df_altitud_NO = df_altitud_NO.rename(columns = {"ALTITUD":"ALTITUD_NO"})

    df_month = df[['datetime', 'value']].groupby(pd.Grouper(key="datetime", freq="H")).mean()
    
    df_month['hour']  = df_month.index.hour
    df_month['day']   = df_month.index.day
    df_month['month'] = df_month.index.month
    df_month['year']  = df_month.index.year

    def encode_time_indicators(data, col, max_val):
        data[f'{col}_sin'] = np.sin(2 * np.pi * data[col]/max_val)
        data[f'{col}_cos'] = np.cos(2 * np.pi * data[col]/max_val)
        return data

    # Cyclic encoding
    df_month = encode_time_indicators(df_month, 'hour', 24)
    df_month = encode_time_indicators(df_month, 'day', 31)
    df_month = encode_time_indicators(df_month, 'month', 12)

    # Construct target
    df_month_NO = df_NO[['datetime', 'value']].groupby(pd.Grouper(key="datetime", freq="H")).mean()
    df_month_NO = df_month_NO.rename(columns = {"value":"value_NO"})

    df_features = pd.concat([df_month, df_altitud_NO, df_altitud, df_area_urbana_NO, df_area_urbana, df_month_NO['value_NO']], axis=1)
    df_features.dropna(inplace=True)

    X = df_features[['value','year','hour_sin','hour_cos','day_sin','day_cos','month_sin','month_cos','ALTITUD_NO','ALTITUD','rural_NO','suburban_NO','urban_NO','rural','suburban','urban']]
    y = df_features['value_NO']

    del df_features, df_month, df_altitud_NO, df_altitud, df_area_urbana_NO, df_area_urbana, df_month_NO
    gc.collect()

    return X, y

def algo(local=False):
    filename = get_input(local)

    if not filename:
        print("Could not retrieve filename.")
        return

    print('Load CSV')
    df = pd.read_csv(filename)
    print('Prepare data')
    df = prepare_data(df)
    print('Create features')
    X, y = get_features(df)

    print('Model')
    # SARIMAX, use additional features (X) to help explain some of the variation in the time series that cannot be explained by the ARIMA model
    model = sm.tsa.SARIMAX(y, exog=X, order=(1, 1, 1))
    results = model.fit()
    print('Prediction for each hour between 2023-02-15 00:00:00 and 2023-02-28 23:00:00')
    pred = results.predict(start=len(y), end=len(y)+815, exog=X[-816:])

    pred_index = pd.date_range(start=y.index[-1], end='2023-02-28 23:00:00', freq='H')
    pred.index = pred_index
    pred_off = pred[(pred.index >= '2023-02-15 00:00:00') & (pred.index < '2023-03-01 00:00:00')]

    filename = "predictions.pickle" if local else "/data/outputs/result"
    with open(filename, "wb") as pickle_file:
        print(f"Pickling results in {filename}")
        pickle.dump(pred_off.values, pickle_file)
    
if __name__ == "__main__":
    local = len(sys.argv) == 2 and sys.argv[1] == "local"
    algo(local)