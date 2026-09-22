import datetime as dt
import json
import locale
import os
import re
import time
import warnings

import jdatetime
import numpy as np
import pandas as pd
import pytz
import requests
from persiantools.jdatetime import JalaliDate

jdatetime.set_locale("fa_IR")
warnings.filterwarnings("ignore")


import time
import statistics
import json
from persiantools.jdatetime import JalaliDate
import datetime 

def convert_ar_characters(input_str):

    mapping = {
        'ك': 'ک',
        'دِ': 'د',
        'بِ': 'ب',
        'زِ': 'ز',
        'ذِ': 'ذ',
        'شِ': 'ش',
        'سِ': 'س',
        'ى': 'ی',
        'ي': 'ی'
    }
    return _multiple_replace(mapping, input_str)

def _multiple_replace(mapping, text):
    pattern = "|".join(map(re.escape, mapping.keys()))
    return re.sub(pattern, lambda m: mapping[m.group()], str(text))

import plotly.express as px
import plotly.graph_objects as go

def market_mapper(code):
    market_mappperdict = {'311' : 'calloption',
                    '309' : 'yellowIFB',
                    '303' : 'secondryIFB',
                    '305' : 'funds' ,
                    '300' : 'Bourse_symbols', '306':'Finance&bounds',
                    '320' : 'IFB_calloptions', '208':'sokook',
                    '312' : 'putoption' , '706':'MorabeheDolati',
                    '301' : 'CityMosharekat' , '701':'Saffron','307':'RealState',
                    '327' : 'CementKala' , '321' :'IFB_putoptions', '380' : 'Funds_Kala',
                    '404' : 'IFB_paaye_Advanceright_hagh','304':'future', 
                    '206' : 'GAMbounds','400':'Advanceright_hagh', '403':'IFB_Advanceright_hagh',
                    '313' : 'bourseKala_other', '308' : 'boursekala_self', '600': 'Tabaee_put'
                    
                    }
    return market_mappperdict.get(code)

def market_columns(number_of_columns):
    
    main_columns = ['id' , 'isin', 'symbol','name','time',
            'first_price','close_price' , 'last_trade','number_trades',
            'volume', 'value','low_price' ,'high_price',
            'yesterday_price', 'eps' , 'base_volume', 'table_id',
            'industry_id' ,'section_code', 'max_allowed_price','min_allowed_price',
            'number_shares', 'type_of_asset']
    
    if number_of_columns == 23:
        return main_columns
    elif number_of_columns == 25:
        return main_columns + ['NAV','openInterest']
    else:
        print(number_of_columns)

def market_fetcher():
    try:
        url_list = ['http://old.tsetmc.com/tsev2/data/MarketWatchInit.aspx?h=0&r=0',
                    'http://old.tsetmc.com/tsev2/data/MarketWatchPlus.aspx?h=0&r=0']
        for _ in range(10):
            import random
            url = random.choice(url_list)
            data = requests.get(url_list[1], timeout=12)
            content = data.content.decode('utf-8')
            parts = content.split('@')
            if data.status_code==200 and len(parts) > 2:
                break
        
    except:
        time.sleep(1)
        pass
    return parts


def base_market_dataframe(parts):
    market = parts[2].split(';')
    while True:
        try:
            number_of_columns = len(market[10].split(','))
            df = pd.DataFrame([x.split(',') for x in market], columns=market_columns(number_of_columns=number_of_columns))
            df['type_of_asset'] = df['type_of_asset'].apply(market_mapper)
            df = df.apply(pd.to_numeric, errors='ignore')
            df['symbol'] = df['symbol'].apply(convert_ar_characters)
            df['name'] = df['name'].apply(convert_ar_characters) 
            break
        except:
            time.sleep(1)
    
    return df

def orderbook_dataframe(parts):
    orderbook = parts[3].split(';')
    columns_orderbook = ['id' , 'location' , 'n_orderSell','n_orderBuy',
                        'bid_price' , 'ask_price' , 'bid_vol','ask_vol']
    df2 = pd.DataFrame([x.split(',') for x in orderbook],columns=columns_orderbook)
    df2_pivot = df2.pivot(index='id' , columns='location')
    df2_pivot.columns = ["_".join((i,j)) for i,j in df2_pivot.columns]

    df2_pivot.reset_index(inplace=True)
    df2_pivot = df2_pivot.apply(pd.to_numeric, errors='ignore')
    return df2_pivot


def type_asset_mapper(assettype : str):
    if assettype=='stocks':
        return ['yellowIFB' , 'secondryIFB','funds',
                'Bourse_symbols']
    elif assettype=='stocks_and_calloptions':
        return ['yellowIFB' , 'secondryIFB','funds',
                'Bourse_symbols' , 'calloption','IFB_calloptions']
    elif assettype=='calloptions':
        return ['calloption','IFB_calloptions']
    elif assettype=='putoptions':
        return ['putoption','IFB_putoptions']
    elif assettype=='stocks_call_put':
        return type_asset_mapper('stocks') + \
    type_asset_mapper('calloptions')+type_asset_mapper('putoptions')
    else :
        return None

def get_all_market(assettype=None):
    
    ''' asset type : stocks , calloptions, 
     stocks_and_calloptions , 
     putoptions and stocks_call_put '''
    
    raw_data= market_fetcher()
    df = base_market_dataframe(raw_data)
    
    df2 = orderbook_dataframe(raw_data)
    df2['id'] = df2['id'].astype(str)
    df['id'] = df['id'].astype(str)
    market = df.merge(df2 ,on='id' )
    
    if assettype:
        return market[market['type_of_asset'].isin(type_asset_mapper(assettype))]
    

    return market

def trade_history_symbol(symbol_id):
    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
    url = f'http://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/{symbol_id}/0'
    data = requests.get(url,timeout=5,verify=False, headers=headers).json()
    columns = ['change_last_price', 'min_price' , 'max_price',
               'yesterday_price' , 'first_price', 'islast' , 'id_' , 'symbol_id',
               'date' , 'time' , 'close_price' , 'iClose_' , 'yClose_','last_price',
               'number_of_trades' , 'volume' , 'value']
    df = pd.DataFrame(data['closingPriceDaily'])
    df.columns = columns
    
    df['date']= pd.to_datetime(df.date,format='%Y%m%d')
    df['time']= pd.to_datetime(df.time,format='%H%M%S').dt.time
    
    # df.set_index('date',inplace=True)

    return df
