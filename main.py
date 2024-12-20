

import json
from transformers import RobertaTokenizer, RobertaModel
# from transformers import AutoTokenizer, RobertaModel
import numpy as np
import argparse
import math
import torch
import torch.nn.functional as F
from tqdm import trange
import datetime
import os, random
from utils.common_utils import Logging

from utils.data_utils import load_data_instances
from data.data_preparing import DataIterator


from modules.models.roberta import Model
from modules.f_loss import FocalLoss

from tools.trainer import Trainer
from tools.evaluate import evaluate
from tools.metric import Metric

from utils.common_utils import stop_words
from transformers import BertTokenizer
from data.data_preparing import Instance



if __name__ == '__main__':
    torch.cuda.set_device(0)

    parser = argparse.ArgumentParser()
    parser.add_argument('--max_sequence_len', type=int, default=100, help='max length of the tagging matrix')
    parser.add_argument('--sentiment2id', type=dict, default={'negative': 2, 'neutral': 3, 'positive': 4}, help='mapping sentiments to ids')
    parser.add_argument('--model_cache_dir', type=str, default='./modules/models/', help='model cache path')
    parser.add_argument('--model_name_or_path', type=str, default='hfl/chinese-roberta-wwm-ext', help='reberta model path')
    # parser.add_argument('--model_name_or_path', type=str, default='roberta-base', help='reberta model path')
    parser.add_argument('--batch_size', type=int, default=16, help='json data path')
    parser.add_argument('--device', type=str, default="cuda", help='gpu or cpu')
    parser.add_argument('--prefix', type=str, default="./data/", help='dataset and embedding path prefix')

    parser.add_argument('--data_version', type=str, default="D1", choices=["D1", "D2"], help='dataset and embedding path prefix')
    parser.add_argument('--dataset', type=str, default="res14", choices=["res14", "lap14", "res15", "res16"], help='dataset')

    parser.add_argument('--bert_feature_dim', type=int, default=768, help='dimension of pretrained bert feature')
    parser.add_argument('--epochs', type=int, default=2000, help='training epoch number')
    parser.add_argument('--class_num', type=int, default=5, help='label number')
    parser.add_argument('--task', type=str, default="triplet", choices=["pair", "triplet"], help='option: pair, triplet')
    parser.add_argument('--model_save_dir', type=str, default="/mnt/md0/chen-wei/zi/MiniConGTS_copy_ch_cantrain/modules/models/saved_models/", help='model path prefix')
    parser.add_argument('--log_path', type=str, default=None, help='log path')

    parser.add_argument('--mode', type=str, default='train', choices=['train', 'predict'], help='模式：train 或 predict')
    parser.add_argument('--input_file', type=str, default='/mnt/md0/chen-wei/zi/MiniConGTS_copy/data/D1/res14/NYCU_NLP_113A_Validation.txt', help='预测模式下的输入文件')
    parser.add_argument('--output_file', type=str, default='submission.txt', help='预测结果输出文件')


    args = parser.parse_known_args()[0]
    if args.log_path is None:
        args.log_path = 'log_{}_{}_{}.log'.format(args.data_version, args.dataset, datetime.datetime.now().strftime('%Y-%m-%d-%H-%M'))
#/mnt/md0/chen-wei/zi/MiniConGTS_copy/log/
    #加载预训练字典和分词方法
    # tokenizer = RobertaTokenizer.from_pretrained(args.model_name_or_path,
    #     cache_dir=args.model_cache_dir,  # 将数据保存到的本地位置，使用cache_dir 可以指定文件下载位置
    #     force_download=False,  # 是否强制下载
    # )

    tokenizer = BertTokenizer.from_pretrained("hfl/chinese-roberta-wwm-ext")
    # tokenizer = RobertaModel.from_pretrained("hfl/chinese-roberta-wwm-ext")

    logging = Logging(file_name=args.log_path).logging


    def seed_torch(seed):
        random.seed(seed)
        np.random.seed(seed)
        os.environ['PYTHONHASHSEED'] = str(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        torch.cuda.manual_seed(seed)
        random.seed(seed)
        
    seed = 666
    seed_torch(seed)
    datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    logging(f"""
            \n\n
            ========= - * - =========
            date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            seed: {seed}
            ========= - * - =========
            """
        )


    # Load Dataset
    # train_sentence_packs = json.load(open(os.path.abspath(args.prefix + args.data_version + '/' + args.dataset + '/train.json')))NYCU_train.json
    train_sentence_packs = json.load(open(os.path.abspath(args.prefix + args.data_version + '/' + args.dataset + '/NYCU_train.json'), encoding='utf-8')) #train_with_intensity.json
    
    # random.shuffle(train_sentence_packs)
    dev_sentence_packs = json.load(open(os.path.abspath(args.prefix + args.data_version + '/' + args.dataset + '/NYCU_dev.json'), encoding='utf-8'))#dev_with_intensity.json
    test_sentence_packs = json.load(open(os.path.abspath(args.prefix + args.data_version + '/' + args.dataset + '/NYCU_test.json'), encoding='utf-8'))#test_with_intensity.json

    train_instances = load_data_instances(tokenizer, train_sentence_packs, args)
    dev_instances = load_data_instances(tokenizer, dev_sentence_packs, args)
    test_instances = load_data_instances(tokenizer, test_sentence_packs, args)

    trainset = DataIterator(train_instances, args)
    devset = DataIterator(dev_instances, args)
    testset = DataIterator(test_instances, args)



    model = Model(args).to(args.device)
    optimizer = torch.optim.Adam([
                    {'params': model.bert.parameters(), 'lr': 1e-5},
                    {'params': model.linear1.parameters(), 'lr': 1e-2},
                    {'params': model.cls_linear.parameters(), 'lr': 1e-3},
                    {'params': model.cls_linear1.parameters(), 'lr': 1e-3},
                    {'params': model.intensity_head.parameters(), 'lr': 1e-2}
                   ], lr=1e-3)#SGD, momentum=0.9  
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[200, 600, 1000], gamma=0.5, verbose=True)


    # label = ['N', 'CTD', 'POS', 'NEU', 'NEG']
    weight = torch.tensor([1.0, 6.0, 6.0, 6.0, 6.0]).float().cuda()
    f_loss = FocalLoss(weight, ignore_index=-1)#.forwardf_loss(preds,labels)

    weight1 = torch.tensor([1.0, 4.0]).float().cuda()
    f_loss1 = FocalLoss(weight1, ignore_index=-1)

    '''
    ## beta_1 和 beta_2 : weight of loss function 
    # 
    # 用於平衡loss1（次級損失函數）和loss_cl（對比損失）在總損失中的貢獻：

    bear_max和last：

        這些參數似乎可以管理使用對比學習時的訓練行為 ( self.contrastive)。
        儘管在提供的函數中註解掉了train，但邏輯表明：
        bear_max可能代表在激活對比學習之前性能沒有提高的連續時期的最大數量。
        last似乎是一個倒數計時器或限制對比學習保持活躍的時期數：

    '''
    
    '''
    下一步
        調整參數beta_1：根據beta_2任務中每個損失函數的相對重要性來選擇值。同樣，設定bear_max並last控制訓練動態。
        取消註釋邏輯：取消方法中相關部分的註釋，以啟動由和train控制的對比學習邏輯。bear_maxlast
        實驗：使用這些參數進行實驗，觀察它們對模型表現和訓練動態的影響。    
    '''
    beta_1 = 1.0  # Weight for loss1
    beta_2 = 0.5  # Weight for contrastive loss
    bear_max = 5  # Maximum patience before enabling contrastive learning
    last = 10     # Duration for which contrastive learning remains active
    # trainer = Trainer(model, trainset, devset, testset, optimizer, (f_loss, f_loss1), lr_scheduler, args, logging)


    
    # Run evaluation

    # Load a pre-trained model for evaluation
    # saved_model_path = "/mnt/md0/chen-wei/zi/MiniConGTS_copy/modules/models/saved_models/best_model_ch.pt"
    # saved_model_path = "/mnt/md0/chen-wei/zi/MiniConGTS_copy/modules/models/saved_models/best_model_eng.pt"
    
    
    # if os.path.exists(saved_model_path):
    #     logging(f"Loading model from {saved_model_path}")
    #     model = torch.load(saved_model_path)
    #     model = model.to(args.device)
    # else:
    #     logging(f"Error: Model file {saved_model_path} does not exist.")
    #     exit(1)

    # # Ensure the model is in evaluation mode
    # model.eval()

    # # Run evaluation
    # precision, recall, f1 = evaluate(model, testset, stop_words, logging, args)
    # logging(f"Precision: {precision}, Recall: {recall}, F1: {f1}")
    
    
    
    ######################
    # def predict_sentences(model, tokenizer,ids, sentences, args):
    #     model.eval()
    #     results = []
    #     batch_size = args.batch_size

    #     # 初始化 Instance 並生成 word_spans
    #     instances = [Instance(tokenizer, {"id": str(idx), "sentence": sentence, "triples": []}, args) 
    #                 for idx, sentence in enumerate(sentences)]
    #     token_ranges = [instance.word_spans for instance in instances]
    #     tokenized_sentences = [instance.tokens for instance in instances]

    #     with torch.no_grad():
    #         all_ids = []
    #         all_preds = []
    #         all_labels = []
    #         # all_lengths = []
    #         all_sens_lengths = []
    #         all_token_ranges = []
    #         all_tokenized = []
    #         all_intensities = []  # 標準化後的真實值 # 反標準化的真實值
    #         all_intensity_pred = []  # 標準化後的預測值 # 反標準化的預測值
        
    #         for batch_start in range(0, len(sentences), batch_size):
    #             id_batch = ids[batch_start:batch_start + batch_size]
    #             batch_sentences = sentences[batch_start:batch_start + batch_size]
    #             batch_token_ranges = token_ranges[batch_start:batch_start + batch_size]
    #             batch_tokenized = tokenized_sentences[batch_start:batch_start + batch_size]

    #             # Tokenize batch
    #             encoded = tokenizer(
    #                 batch_sentences,
    #                 padding="max_length",
    #                 truncation=True,
    #                 max_length=args.max_sequence_len,
    #                 return_tensors='pt'
    #             )

    #             tokens_tensor = encoded['input_ids'].to(args.device)
    #             attention_mask = encoded['attention_mask'].to(args.device)

    #             # Construct masks_tensor for batched input
    #             masks_tensor = attention_mask.unsqueeze(1) * attention_mask.unsqueeze(2)

    #             # Model inference
    #             logits, _, _, intensity_pred = model(tokens_tensor, masks_tensor)
    #             # print(f"intensity_logits shape: {intensity_pred.shape}")
    #             preds = torch.argmax(logits, dim=3) #2
    #             all_preds.append(preds) #3
    #             all_labels.append(preds) #4
    #             # all_lengths.append(lengths) #5
    #             sens_lens = [len(token_range) for token_range in token_ranges]
    #             all_sens_lengths.extend(sens_lens) #6
    #             all_token_ranges.extend(token_ranges) #7
    #             all_ids.extend(id_batch) #8
    #             all_tokenized.extend(batch_tokenized)
    #             # intensity 處  
    #             all_intensity_pred.append(intensity_pred)
    #             all_intensities.append(intensity_pred)
    #             # Process predictions and intensities
    #             # for idx, sentence in enumerate(batch_sentences):
    #             #     pred_matrix = preds[idx].cpu().numpy()
    #             #     intensity_matrix = []
    #             #     print(f"intensity_logits[idx] shape: {intensity_logits[idx].shape}")

    #             #     valence_pred = intensity_logits[idx][..., 0].max().item()
    #             #     arousal_pred = intensity_logits[idx][..., 1].max().item()
    #             #     print(f"valence_pred: {valence_pred}")
    #             #     print(f"arousal_pred: {arousal_pred}")

    #             #     intensity_matrix.append([valence_pred, arousal_pred])
    #             #     # Collect results for metrics
    #             #     all_preds.append(pred_matrix)
    #             #     all_ids.append(str(batch_start + idx))
    #             #     all_tokenized.append(batch_tokenized[idx])
    #             #     all_sens_lengths.append(len(batch_token_ranges[idx]))
    #             #     all_token_ranges.append(batch_token_ranges[idx])
    #             #     all_intensities.append([[0.0, 0.0]])  # Placeholder for actual values
    #             #     all_intensity_pred.append(intensity_matrix)
    #         all_preds = torch.cat([torch.tensor(p) for p in all_preds], dim=0).cpu().tolist()
    #         all_labels = torch.cat([torch.tensor(l) for l in all_labels], dim=0).cpu().tolist()
    #         all_intensity_pred = torch.cat([torch.tensor(ip) for ip in all_intensity_pred], dim=0).cpu().tolist()
    #         all_intensities = torch.cat([torch.tensor(i) for i in all_intensities], dim=0).cpu().tolist()

    #         # Metric calculation
    #         metric = Metric(
    #             args,
    #             stop_words,
    #             all_tokenized,
    #             all_ids,
    #             all_preds,
    #             all_preds,  # Placeholder labels (if not available)
    #             all_sens_lengths,
    #             all_token_ranges,
    #             all_intensities,
    #             all_intensity_pred
    #         )

    #         p_predicted_set, _, _ = metric.get_sets()

    #         triplets = []
    #         for triplet_info in p_predicted_set:
    #             # 擷取 aspect 和 opinion 的 indices 與強度值
    #             tokens = triplet_info['tokens']
    #             aspect_indices = triplet_info['aspect_indices']
    #             opinion_indices = triplet_info['opinion_indices']
    #             intensity = triplet_info['intensity']

    #             # 強度值放大並格式化
    #             # print(f"Original intensity: {intensity}")
    #             # scaled_intensity = [val * 10 for val in intensity]
    #             # print(f"Scaled intensity: {scaled_intensity}")

    #             # 提取 tokens 中的 aspect 和 opinion 詞彙
    #             aspect_words = tokens[aspect_indices[0]-1:aspect_indices[1] ]
    #             opinion_words = tokens[opinion_indices[0]:opinion_indices[1] + 1]

    #             # 將強度轉為字串格式
    #             intensity_str = '#'.join([f"{val:.2f}" for val in intensity])
    #             # print(f"Formatted intensity string: {intensity_str}")

    #             # 組合 triplet 並新增到 triplets 列表中
    #             triplets.append({
    #                 # 'id'
    #                 'aspect': ' '.join(aspect_words),  # 合併成字串
    #                 'opinion': ' '.join(opinion_words),  # 合併成字串
    #                 'intensity': intensity_str
    #             })
    #             # print(f"triplets :{triplets}")
    #         # 組合句子與 triplets，並新增到結果中
    #         results.append({
    #             'id': id_,
    #             'sentence': batch_sentences,
    #             'triplets': triplets
    #         })

    #     return results

    import torch
    import numpy as np

    def create_instances_for_prediction(tokenizer, ids, sentences, args):
        instances = []
        for id_, sentence in zip(ids, sentences):
            # 將triples空集合，使Instance不報錯
            single_sentence_pack = {
                'id': id_,
                'sentence': sentence,
                'triples': []  # 預測模式無標註資料
            }
            instance = Instance(tokenizer, single_sentence_pack, args)
            instances.append(instance)
        return instances

    def find_triplet(tag, ws, tokenized, predicted_intensities_matrix, sentiment2id, stop_words=[]):
        triplets = []
        for row in range(1, tag.shape[0]-1):
            for col in range(1, tag.shape[1]-1):
                if row == col:
                    pass
                elif tag[row][col] in sentiment2id.values():
                    # 取得評價情緒標籤，但我們最終不輸出
                    # sentiment = int(tag[row][col])
                    al, pl = row, col
                    ar = al
                    pr = pl
                    # 展開 aspect 與 opinion 範圍
                    while ar+1 < tag.shape[0] and tag[ar+1][pr] == 1:
                        ar += 1
                    while pr+1 < tag.shape[1] and tag[ar][pr+1] == 1:
                        pr += 1

                    # 從預測的 intensity 中計算 v、a
                    # 原本是四捨五入並轉int，現在直接保留浮點並格式化輸出
                    sub_matrix_0 = predicted_intensities_matrix[al:ar+1, pl:pr+1, 0]
                    sub_matrix_1 = predicted_intensities_matrix[al:ar+1, pl:pr+1, 1]
                    v = sub_matrix_0.mean().item() * 10  # 將原本0~1範圍的值乘10
                    a = sub_matrix_1.mean().item() * 10

                    aspect_text = "".join(tokenized[al:ar+1])
                    opinion_text = "".join(tokenized[pl:pr+1])

                    # 不再存 sentiment，直接存回 (aspect, opinion, v, a)
                    triplets.append((aspect_text, opinion_text, v, a))

        return triplets


    def predict_sentences(model, tokenizer, ids, sentences, args, output_file=None):
        model.eval()
        device = args.device
        stop_words = []
        sentiment2id = args.sentiment2id

        # 建立預測用的 instances
        instances = create_instances_for_prediction(tokenizer, ids, sentences, args)
        dataset = DataIterator(instances, args)

        results = []
        with torch.no_grad():
            for i in range(dataset.batch_count):
                (sentence_ids, 
                bert_tokens, 
                masks, 
                word_spans, 
                tagging_matrices, 
                tokenized, 
                cl_masks, 
                token_classes, 
                intensity_tagging_matrices) = dataset.get_batch(i)

                logits, logits1, sim_matrix, intensity_pred = model(bert_tokens, masks)
                preds = torch.argmax(logits, dim=3).cpu().numpy()  # [batch, seq_len, seq_len]
                intensity_pred = intensity_pred.cpu().numpy()       # [batch, seq_len, seq_len, 2]

                for b_idx, id_ in enumerate(sentence_ids):
                    tag = preds[b_idx]

                    # 邊界位置設為 -1
                    tag[0, :] = -1
                    tag[-1, :] = -1
                    tag[:, 0] = -1
                    tag[:, -1] = -1

                    predicted_triplets = find_triplet(tag,
                                                    word_spans[b_idx],
                                                    tokenized[b_idx],
                                                    intensity_pred[b_idx],
                                                    sentiment2id,
                                                    stop_words)

                    # 格式化輸出 (aspect, opinion, v#a)
                    # v, a 以小數點後兩位格式化，並以#分隔
                    triplet_str = "".join([
                        f"({asp},{op},{v:.2f}#{a:.2f})"
                        for (asp, op, v, a) in predicted_triplets
                    ])

                    results.append({"id": id_, "triplets_str": triplet_str})

        if output_file is not None:
            with open(output_file, 'w', encoding='utf-8') as f:
                # 無需列出標題行，如需可自行新增
                f.write(f"ID Triplets\n")
                for res in results[1:]:  # 跳過第一個元素                    
                    # 最終格式範例：
                    # E0002:S002 (餐點,美味,6.63#4.63)(上菜速度,飛快,7.25#6.00)
                    # 此處 res["triplets_str"] 已經整理好
                    f.write(f"{res['id']} {res['triplets_str']}\n")

        return results


# 使用範例:
# model = torch.load(saved_model_path)
# model.to(args.device)
# ids = ["E0002:S002", "R3453:S019"]
# sentences = ["餐點真的很美味，上菜速度也很快！", "蝦子肉質相當好而且鮮甜"]
# predict_results = predict_sentences(model, tokenizer, ids, sentences, args, output_file="prediction_results.txt")
# print(predict_results)

# 輸出示例:
# E0002:S002 (餐點,美味,6.63#4.63)(上菜速度,飛快,7.25#6.00)
# R3453:S019 (蝦子肉質,好,4.00#6.00)(蝦子肉質,鮮甜,4.00#6.00)

    if args.mode == 'train':
        # Run train
        trainer = Trainer(model, trainset, devset, testset, optimizer, (f_loss, f_loss1), lr_scheduler, args, logging, beta_1, beta_2, bear_max, last)
        trainer.train()

    elif args.mode == 'predict':
        # Load the pre-trained model
        # saved_model_path = os.path.join(args.model_save_dir, "best_model_ch_best.pt")
        saved_model_path = os.path.join(r"E:\NYCU-Project\Class\NLP\MiniConGTS-chinese\modules\models\saved_models\best_model_ch.pt")
        
        if not os.path.exists(saved_model_path):
            raise FileNotFoundError(f"模型文件 {saved_model_path} 未找到。")
        model = torch.load(saved_model_path)
        model = model.to(args.device)
        model.eval()

        # Load input sentences
        ## 需改動:
        with open(r"E:\NYCU-Project\Class\NLP\MiniConGTS-chinese\data\D1\res14\NYCU_NLP_113A_Test.txt", 'r', encoding='utf-8') as f:
            lines = [line.strip().split(',', 1) for line in f.readlines()]

        if not all(len(line) == 2 for line in lines):
            raise ValueError("输入文件中的每一行必须包含两个逗号分隔的列：'ID, Sentence'。")

        ids = [line[0] for line in lines]
        sentences = [line[1] for line in lines]

        # Process sentences in batches
        predict_results = predict_sentences(model, tokenizer, ids, sentences, args, output_file=args.output_file)
        # print(predict_results)